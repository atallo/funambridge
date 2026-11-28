// Package notify sends alerts to Telegram (optional: bot token + chat id in
// the config or the panel). Each kind of alert is sent at most once per hour.
package notify

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sync"
	"time"

	"github.com/atallo/funambridge/internal/logx"
)

var log = logx.New("telegram")

type Telegram struct {
	mu     sync.Mutex
	token  string
	chat   string
	last   map[string]time.Time
	client *http.Client
}

func New() *Telegram {
	return &Telegram{last: map[string]time.Time{}, client: &http.Client{Timeout: 20 * time.Second}}
}

func (t *Telegram) Configure(token, chat string) {
	t.mu.Lock()
	t.token, t.chat = token, chat
	t.mu.Unlock()
}

func (t *Telegram) Enabled() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.token != "" && t.chat != ""
}

// Alert sends text unless an alert with the same key went out in the last
// hour. Non-blocking.
func (t *Telegram) Alert(key, text string) {
	t.mu.Lock()
	if t.token == "" || t.chat == "" || time.Since(t.last[key]) < time.Hour {
		t.mu.Unlock()
		return
	}
	t.last[key] = time.Now()
	t.mu.Unlock()
	go func() {
		if err := t.Send(context.Background(), text); err != nil {
			log.Warnf("no se pudo enviar el aviso: %v", err)
		}
	}()
}

// Send posts a message now (used by the panel's "probar" button).
func (t *Telegram) Send(ctx context.Context, text string) error {
	t.mu.Lock()
	token, chat := t.token, t.chat
	t.mu.Unlock()
	if token == "" || chat == "" {
		return fmt.Errorf("Telegram no está configurado")
	}
	body, _ := json.Marshal(map[string]any{"chat_id": chat, "text": "funambridge: " + text,
		"disable_web_page_preview": true})
	req, _ := http.NewRequestWithContext(ctx, "POST",
		"https://api.telegram.org/bot"+token+"/sendMessage", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := t.client.Do(req)
	if err != nil {
		return fmt.Errorf("conexión con Telegram: %v", logx.Redact(err.Error()))
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode != 200 {
		var e struct {
			Description string `json:"description"`
		}
		_ = json.Unmarshal(b, &e)
		return fmt.Errorf("Telegram respondió %d: %s", resp.StatusCode, e.Description)
	}
	return nil
}
