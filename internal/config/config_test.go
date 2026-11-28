package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadExample(t *testing.T) {
	c, err := Load(filepath.Join("..", "..", "config.example.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if c.Port != 9000 || c.KeepaliveMinutes != 5 || len(c.Accounts) != 1 || c.MaxUploads != 0 {
		t.Fatalf("ejemplo mal leído: %+v", c)
	}
}

// A config written by the Python versions (0.x): "s3:" instead of "server:",
// fields the Go version ignores, no transfers/telegram.
const pythonConfig = `s3:
  host: 0.0.0.0
  port: 9000
admin:
  enabled: true
verify_s3_signature: true
log_file: /data/funambridge.log
root_bucket: raiz
tls:
  cert: ''
  key: ''
cache_dir: /data/cache
keepalive_minutes: 5
accounts:
- name: prueba
  base_url: https://cloud.o2online.es
  access_key: O2AAAAAAAAAAAAAAAAAA
  secret_key: bbbb
  device_id: web-0123456789abcdef0123456789abcdef
  s3_enabled: true
  webdav_enabled: true
  cache_seconds: 29
  oauth:
    accesstoken: ''
    refreshtoken: ''
    platform: web
    expiresin: ''
    lastrefreshdate: 0
    msisdn: '+34600112233'
    captured_at: ''
  session:
    jsessionid: JJJ
    validationkey: VVV
    plc: ''
    captured_at: '2026-11-28T12:27:42Z'
`

func TestLoadPythonConfigAndSave(t *testing.T) {
	p := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(p, []byte(pythonConfig), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	a := c.ByUser("O2AAAAAAAAAAAAAAAAAA")
	if c.Port != 9000 || c.LogFile != "/data/funambridge.log" || a == nil || a.SecretKey != "bbbb" ||
		a.Session.JSessionID != "JJJ" || a.Session.ValidationKey != "VVV" ||
		a.DeviceID != "web-0123456789abcdef0123456789abcdef" || a.Phone() != "+34600112233" {
		t.Fatalf("config 0.x mal leída: %+v %+v", c, a)
	}
	if err := c.Save(); err != nil {
		t.Fatal(err)
	}
	c2, err := Load(p)
	if err != nil {
		t.Fatal(err)
	}
	if c2.Port != 9000 || c2.ByName("prueba").Session.ValidationKey != "VVV" {
		t.Fatal("no sobrevive a guardar y releer")
	}
}
