// funambridge -- accede a O2 Cloud (Funambol OneMediaHub) por WebDAV.
//
//	funambridge [--config config.yaml] [-v] [--log-file f] <subcomando>
//
//	serve        sirve WebDAV + panel web (por defecto)
//	import-har   importa una sesión web desde un fichero HAR
//	probe        prueba la sesión de una cuenta
//	dedupe       busca/repara copias duplicadas (simulación salvo --apply)
//	health       comprueba que el servidor responde (healthcheck de Docker)
package main

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/atallo/funambridge/internal/config"
	"github.com/atallo/funambridge/internal/har"
	"github.com/atallo/funambridge/internal/logx"
	"github.com/atallo/funambridge/internal/repair"
	"github.com/atallo/funambridge/internal/sapi"
	"github.com/atallo/funambridge/internal/server"
	"github.com/atallo/funambridge/internal/tlsutil"
	"github.com/atallo/funambridge/internal/version"
)

var mlog = logx.New("funambridge")

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func die(format string, a ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", a...)
	os.Exit(1)
}

func main() {
	g := flag.NewFlagSet("funambridge", flag.ExitOnError)
	cfgPath := g.String("config", env("FUNAMBRIDGE_CONFIG", "config.yaml"), "ruta a config.yaml")
	verbose := g.Bool("v", false, "DEBUG también por consola")
	logFile := g.String("log-file", env("FUNAMBRIDGE_LOG_FILE", ""), "log extendido (DEBUG) en este fichero")
	showVer := g.Bool("version", false, "muestra la versión")
	g.Usage = func() {
		fmt.Fprintf(os.Stderr, "funambridge %s\n\nuso: funambridge [opciones] [serve|import-har|probe|dedupe|health] ...\n\n", version.Version)
		g.PrintDefaults()
	}
	_ = g.Parse(os.Args[1:])
	if *showVer {
		fmt.Println(version.Version)
		return
	}
	logx.SetVerbose(*verbose)
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		die("config: %v", err)
	}
	if *logFile == "" {
		*logFile = cfg.LogFile
	}
	args := g.Args()
	cmd := "serve"
	if len(args) > 0 {
		cmd, args = args[0], args[1:]
	}
	if *logFile != "" && cmd == "serve" {
		if err := logx.SetFile(*logFile); err != nil {
			die("log: %v", err)
		}
	}
	switch cmd {
	case "serve":
		serve(cfg, *logFile, args)
	case "import-har":
		importHAR(cfg, args)
	case "probe":
		probe(cfg, args)
	case "dedupe":
		dedupe(cfg, args)
	case "health":
		health(cfg)
	default:
		g.Usage()
		os.Exit(2)
	}
}

func serve(cfg *config.Config, logFile string, args []string) {
	f := flag.NewFlagSet("serve", flag.ExitOnError)
	host := f.String("host", "", "dirección de escucha (por defecto la del config)")
	port := f.Int("port", 0, "puerto (por defecto el del config)")
	cert := f.String("tls-cert", "", "certificado PEM para servir HTTPS (autofirmado si no existe)")
	key := f.String("tls-key", "", "clave PEM para HTTPS")
	_ = f.Parse(args)
	if *host != "" {
		cfg.Host = *host
	}
	if *port != 0 {
		cfg.Port = *port
	}
	if *cert != "" {
		cfg.TLSCert = *cert
	}
	if *key != "" {
		cfg.TLSKey = *key
	}
	if logFile != "" {
		mlog.Infof("log extendido (DEBUG) -> %s", logFile)
	}
	reg := server.NewRegistry(cfg)
	reg.StartKeepalive()
	addr := net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port))
	srv := &http.Server{Addr: addr, Handler: server.New(reg),
		ReadHeaderTimeout: 60 * time.Second, IdleTimeout: 120 * time.Second}
	scheme := "http"
	if cfg.TLSCert != "" && cfg.TLSKey != "" {
		tc, err := tlsutil.Config(cfg.TLSCert, cfg.TLSKey)
		if err != nil {
			mlog.Errorf("TLS deshabilitado (%v); sirviendo HTTP", err)
		} else {
			srv.TLSConfig, scheme = tc, "https"
			srv.TLSNextProto = map[string]func(*http.Server, *tls.Conn, http.Handler){} // HTTP/1.1
		}
	}
	if cfg.Host != "127.0.0.1" && cfg.Host != "localhost" && cfg.Host != "::1" {
		mlog.Warnf("escuchando en %s: accesible desde tu red/Docker. Ponlo detrás de TLS/tu propia auth en redes no confiables.", cfg.Host)
	}
	base := fmt.Sprintf("%s://%s", scheme, addr)
	mlog.Infof("funambridge %s en %s", version.Version, base)
	mlog.Infof("  panel  : %s/_admin/", base)
	mlog.Infof("  WebDAV : %s  (%d cuenta(s))", base, len(cfg.Accounts))
	for _, a := range cfg.Accounts {
		auth := "SIN SESIÓN"
		if a.HasAuth() {
			auth = "sesión"
		}
		mlog.Infof("    '%s'  usuario WebDAV=%s  %s", a.Name, a.AccessKey, auth)
	}
	if len(cfg.Accounts) == 0 {
		mlog.Infof("  No hay cuentas: abre %s/_admin/ para añadir una.", base)
	}
	if cfg.MaxUploads > 0 || cfg.MaxDownloads > 0 {
		mlog.Infof("  paralelismo: %d subidas / %d descargas a la vez (0 = sin límite)", cfg.MaxUploads, cfg.MaxDownloads)
	}

	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
		<-ch
		mlog.Infof("parando")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	}()
	if scheme == "https" {
		err := srv.ListenAndServeTLS("", "")
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			die("servidor: %v", err)
		}
		return
	}
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		die("servidor: %v", err)
	}
}

func importHAR(cfg *config.Config, args []string) {
	f := flag.NewFlagSet("import-har", flag.ExitOnError)
	name := f.String("name", "", "nombre de la cuenta")
	_ = f.Parse(args)
	if f.NArg() < 1 {
		die("uso: funambridge import-har [--name N] fichero.har")
	}
	b, err := os.ReadFile(f.Arg(0))
	if err != nil {
		die("no se pudo leer el HAR: %v", err)
	}
	s := har.Parse(string(b))
	if *name == "" {
		*name = "o2"
		if s.Msisdn != "" {
			*name = "o2-" + s.Msisdn
		}
	}
	a, err := cfg.UpsertSession(*name, s.JSessionID, s.ValidationKey, s.PLC, s.BaseURL, s.DeviceID)
	if err != nil {
		die("no se pudo importar el HAR: %v", err)
	}
	if s.Msisdn != "" {
		a.OAuth.Msisdn = s.Msisdn
	}
	if err := cfg.Save(); err != nil {
		die("%v", err)
	}
	fmt.Printf("Sesión importada para la cuenta '%s'.\n  usuario WebDAV    = %s\n  contraseña WebDAV = %s\n", a.Name, a.AccessKey, a.SecretKey)
	fmt.Println("Prueba:  funambridge probe " + a.Name)
}

func account(cfg *config.Config, name string) *config.Account {
	if name != "" {
		if a := cfg.ByName(name); a != nil {
			return a
		}
		die("no existe la cuenta '%s'", name)
	}
	if len(cfg.Accounts) == 0 {
		die("no hay cuentas en %s", cfg.Path)
	}
	return cfg.Accounts[0]
}

func probe(cfg *config.Config, args []string) {
	name := ""
	if len(args) > 0 {
		name = args[0]
	}
	a := account(cfg, name)
	if !a.HasAuth() {
		die("la cuenta '%s' no tiene sesión", a.Name)
	}
	c := sapi.New(a.BaseURL, a.DeviceID, &a.Session, func() { _ = cfg.Save() })
	ctx := context.Background()
	fmt.Printf("== probe '%s' (%s) ==\n", a.Name, a.BaseURL)
	if err := c.Profile(ctx); err != nil {
		die("sesión: NO VÁLIDA (%v)", err)
	}
	fmt.Println("sesión: VÁLIDA · huella TLS:", sapi.FingerprintName())
	fmt.Printf("límites de O2: %+v · subidas en %s\n", c.Limits(ctx), c.UploadBase(ctx))
	if st, err := c.Storage(ctx); err == nil {
		fmt.Printf("espacio: %.1f GB usados de %.1f GB (papelera %.1f GB)\n",
			float64(st.Used)/1e9, float64(st.Quota)/1e9, float64(st.Trash)/1e9)
	}
}

func dedupe(cfg *config.Config, args []string) {
	f := flag.NewFlagSet("dedupe", flag.ExitOnError)
	acc := f.String("account", "", "cuenta")
	harPath := f.String("har", "", "usar la sesión de este HAR")
	apply := f.Bool("apply", false, "aplicar los cambios (si no, solo informa)")
	_ = f.Parse(args)
	if f.NArg() < 1 {
		die("uso: funambridge dedupe [--account A | --har F] [--apply] carpeta (p. ej. backup/NAS.hbk)")
	}
	var c *sapi.Client
	if *harPath != "" {
		b, err := os.ReadFile(*harPath)
		if err != nil {
			die("%v", err)
		}
		s := har.Parse(string(b))
		sess := config.Session{JSessionID: s.JSessionID, ValidationKey: s.ValidationKey, PLC: s.PLC}
		c = sapi.New(s.BaseURL, s.DeviceID, &sess, nil)
	} else {
		a := account(cfg, *acc)
		c = sapi.New(a.BaseURL, a.DeviceID, &a.Session, func() { _ = cfg.Save() })
	}
	if _, err := repair.Run(context.Background(), c, f.Arg(0), *apply, func(s string) { fmt.Println(s) }); err != nil {
		die("%v", err)
	}
}

func health(cfg *config.Config) {
	host := cfg.Host
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	c, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(cfg.Port)), 3*time.Second)
	if err != nil {
		die("no responde: %v", err)
	}
	c.Close()
}
