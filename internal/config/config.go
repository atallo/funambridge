// Package config is the YAML configuration of funambridge.
//
// It reads the files written by the Python versions (0.x) unchanged, so an
// installation can switch to the Go version without logging in again:
// "server:" (or the older "s3:") host/port, accounts with access_key/secret_key
// (= WebDAV user/password), device_id and the O2 web session.
package config

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	"gopkg.in/yaml.v3"
)

const DefaultBaseURL = "https://cloud.o2online.es"

type OAuth struct {
	AccessToken     string `yaml:"accesstoken"`
	RefreshToken    string `yaml:"refreshtoken"`
	Platform        string `yaml:"platform"`
	ExpiresIn       string `yaml:"expiresin"`
	LastRefreshDate int64  `yaml:"lastrefreshdate"`
	Msisdn          string `yaml:"msisdn"`
	CapturedAt      string `yaml:"captured_at"`
}

// Session is the O2 web session (cookies JSESSIONID + validationKey, plus the
// key echoed as a query parameter).
type Session struct {
	JSessionID    string `yaml:"jsessionid"`
	ValidationKey string `yaml:"validationkey"`
	PLC           string `yaml:"plc"`
	CapturedAt    string `yaml:"captured_at"`
}

func (s Session) IsSet() bool { return s.JSessionID != "" && s.ValidationKey != "" }

type Account struct {
	Name         string  `yaml:"name"`
	BaseURL      string  `yaml:"base_url"`
	AccessKey    string  `yaml:"access_key"` // WebDAV user
	SecretKey    string  `yaml:"secret_key"` // WebDAV password
	DeviceID     string  `yaml:"device_id"`
	CacheSeconds int     `yaml:"cache_seconds"` // kept for 0.x configs (unused in 1.x)
	OAuth        OAuth   `yaml:"oauth"`
	Session      Session `yaml:"session"`
}

func (a *Account) HasAuth() bool { return a.Session.IsSet() }
func (a *Account) Phone() string { return a.OAuth.Msisdn }

type Config struct {
	Path string `yaml:"-"`

	Host             string
	Port             int
	AdminEnabled     bool
	LogFile          string
	TLSCert, TLSKey  string
	CacheDir         string // where PUT bodies are spooled ("" = system temp)
	KeepaliveMinutes int
	// Parallelism towards O2. 0 = unlimited (default): every request goes
	// straight through, like 0.x did.
	MaxUploads   int
	MaxDownloads int
	// Upload pictures/videos/audio as such (O2 may process them as media).
	// false (default): everything goes up as a generic file, untouched.
	UploadAsMedia bool
	Accounts      []*Account

	mu sync.Mutex
}

// file is the on-disk shape.
type file struct {
	Server *struct {
		Host string `yaml:"host"`
		Port int    `yaml:"port"`
	} `yaml:"server,omitempty"`
	S3 *struct { // 0.x
		Host string `yaml:"host"`
		Port int    `yaml:"port"`
	} `yaml:"s3,omitempty"`
	Admin *struct {
		Enabled *bool `yaml:"enabled"`
	} `yaml:"admin,omitempty"`
	LogFile string `yaml:"log_file"`
	TLS     struct {
		Cert string `yaml:"cert"`
		Key  string `yaml:"key"`
	} `yaml:"tls"`
	CacheDir         string     `yaml:"cache_dir"`
	KeepaliveMinutes *int       `yaml:"keepalive_minutes"`
	Transfers        *transfers `yaml:"transfers,omitempty"`
	UploadAsMedia    bool       `yaml:"upload_as_media"`
	Accounts         []*Account `yaml:"accounts"`
}

type transfers struct {
	MaxUploads   int `yaml:"max_uploads"`
	MaxDownloads int `yaml:"max_downloads"`
}

func defaults(path string) *Config {
	return &Config{Path: path, Host: "0.0.0.0", Port: 9000, AdminEnabled: true,
		KeepaliveMinutes: 5}
}

// Load reads path; a missing file gives the defaults (no accounts).
func Load(path string) (*Config, error) {
	c := defaults(path)
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	if err != nil {
		return nil, err
	}
	var f file
	if err := yaml.Unmarshal(b, &f); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	switch {
	case f.Server != nil:
		c.Host, c.Port = f.Server.Host, f.Server.Port
	case f.S3 != nil:
		c.Host, c.Port = f.S3.Host, f.S3.Port
	}
	if c.Host == "" {
		c.Host = "0.0.0.0"
	}
	if c.Port == 0 {
		c.Port = 9000
	}
	if f.Admin != nil && f.Admin.Enabled != nil {
		c.AdminEnabled = *f.Admin.Enabled
	}
	c.LogFile, c.TLSCert, c.TLSKey, c.CacheDir = f.LogFile, f.TLS.Cert, f.TLS.Key, f.CacheDir
	if f.KeepaliveMinutes != nil {
		c.KeepaliveMinutes = *f.KeepaliveMinutes
	}
	if f.Transfers != nil {
		c.MaxUploads, c.MaxDownloads = f.Transfers.MaxUploads, f.Transfers.MaxDownloads
	}
	c.UploadAsMedia = f.UploadAsMedia
	for _, a := range f.Accounts {
		if a == nil {
			continue
		}
		if a.Name == "" {
			a.Name = "account"
		}
		if a.BaseURL == "" {
			a.BaseURL = DefaultBaseURL
		}
		if a.AccessKey == "" {
			a.AccessKey = NewUser()
		}
		if a.SecretKey == "" {
			a.SecretKey = NewPassword()
		}
		if a.DeviceID == "" {
			a.DeviceID = NewDeviceID()
		}
		c.Accounts = append(c.Accounts, a)
	}
	return c, nil
}

// Save writes the config atomically (temp file + rename).
func (c *Config) Save() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	ka := c.KeepaliveMinutes
	f := file{LogFile: c.LogFile, CacheDir: c.CacheDir, KeepaliveMinutes: &ka,
		UploadAsMedia: c.UploadAsMedia, Accounts: c.Accounts}
	f.Server = &struct {
		Host string `yaml:"host"`
		Port int    `yaml:"port"`
	}{c.Host, c.Port}
	en := c.AdminEnabled
	f.Admin = &struct {
		Enabled *bool `yaml:"enabled"`
	}{&en}
	f.TLS.Cert, f.TLS.Key = c.TLSCert, c.TLSKey
	f.Transfers = &transfers{c.MaxUploads, c.MaxDownloads}
	b, err := yaml.Marshal(&f)
	if err != nil {
		return err
	}
	tmp := c.Path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, c.Path)
}

// Lock/Unlock guard multi-step changes to the accounts list.
func (c *Config) Lock()   { c.mu.Lock() }
func (c *Config) Unlock() { c.mu.Unlock() }

func (c *Config) ByName(n string) *Account {
	for _, a := range c.Accounts {
		if a.Name == n {
			return a
		}
	}
	return nil
}

// ByUser finds the account for a WebDAV user (its access_key, or its name).
func (c *Config) ByUser(u string) *Account {
	for _, a := range c.Accounts {
		if a.AccessKey == u {
			return a
		}
	}
	return c.ByName(u)
}

func (c *Config) AddAccount(name string) *Account {
	a := &Account{Name: name, BaseURL: DefaultBaseURL, AccessKey: NewUser(),
		SecretKey: NewPassword(), DeviceID: NewDeviceID(), CacheSeconds: 15}
	c.Accounts = append(c.Accounts, a)
	return a
}

func (c *Config) RemoveAccount(name string) {
	out := c.Accounts[:0]
	for _, a := range c.Accounts {
		if a.Name != name {
			out = append(out, a)
		}
	}
	c.Accounts = out
}

// UpsertSession creates/updates an account with a web session.
func (c *Config) UpsertSession(name, jsessionid, vk, plc, baseURL, deviceID string) (*Account, error) {
	jsessionid = strings.TrimSpace(jsessionid)
	if strings.HasPrefix(strings.ToLower(jsessionid), "jsessionid=") {
		jsessionid = jsessionid[len("jsessionid="):]
	}
	vk = strings.TrimSpace(vk)
	if jsessionid == "" {
		return nil, errors.New("JSESSIONID vacío")
	}
	if vk == "" {
		return nil, errors.New("validationkey vacío (necesario para autenticar)")
	}
	if name == "" {
		name = "o2"
	}
	a := c.ByName(name)
	if a == nil {
		a = c.AddAccount(name)
	}
	if baseURL != "" {
		a.BaseURL = baseURL
	}
	if d := strings.TrimSpace(deviceID); d != "" {
		a.DeviceID = d
	}
	a.Session = Session{JSessionID: jsessionid, ValidationKey: vk,
		PLC: strings.TrimSpace(plc), CapturedAt: Now()}
	return a, nil
}

func Now() string { return time.Now().UTC().Format("2006-01-02T15:04:05Z") }

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func NewUser() string     { return "O2" + strings.ToUpper(randHex(9)) }
func NewPassword() string { return randHex(20) }

// NewDeviceID has the format of the O2 web client ("web-<32 hex>"); O2 binds
// a session to the device it was created for.
func NewDeviceID() string { return "web-" + randHex(16) }
