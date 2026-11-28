// Package har extracts a usable O2 web session from a HAR capture of a login
// at cloud.o2online.es. It scans the raw text (no strict JSON parsing), so it
// tolerates large or slightly truncated exports.
//
// A HAR of a login contains LIVE session secrets -- keep it private.
package har

import (
	"net/url"
	"regexp"
	"strings"
)

var (
	cookieRe = regexp.MustCompile(`"name"\s*:\s*"[Cc]ookie"\s*,\s*"value"\s*:\s*"([^"\\]+)"`)
	hostRe   = regexp.MustCompile(`https://([a-zA-Z0-9.-]*o2online\.es)`)
	// the browser's device id: O2's upload host only accepts the device the
	// web session was created for
	deviceRe = regexp.MustCompile(`"name"\s*:\s*"[Xx]-[Dd]evice[Ii][Dd]"\s*,\s*"value"\s*:\s*"([^"\\]+)"`)
	vkRe     = regexp.MustCompile(`validationkey=([0-9a-fA-F]{8,})`)
	msisdnRe = regexp.MustCompile(`[?&]msisdn=([^"&]+)`)
)

type Session struct {
	JSessionID, ValidationKey, PLC, BaseURL, Msisdn, DeviceID string
}

func cookieVal(h, name string) string {
	re := regexp.MustCompile(`(?:^|;\s*)` + regexp.QuoteMeta(name) + `=([^;]+)`)
	if m := re.FindStringSubmatch(h); m != nil {
		return strings.TrimSpace(m[1])
	}
	return ""
}

func Parse(text string) Session {
	s := Session{BaseURL: "https://cloud.o2online.es"}
	for _, m := range hostRe.FindAllStringSubmatch(text, -1) {
		if !strings.HasPrefix(m[1], "upload.") { // the web host, not upload.<host>
			s.BaseURL = "https://" + m[1]
			break
		}
	}
	if d := deviceRe.FindAllStringSubmatch(text, -1); len(d) > 0 {
		s.DeviceID = d[len(d)-1][1] // most recent = current session
	}
	headers := cookieRe.FindAllStringSubmatch(text, -1)
	src := ""
	for _, h := range headers {
		if strings.Contains(h[1], "validationKey=") {
			src = h[1] // prefer the latest authenticated request
		}
	}
	if src == "" && len(headers) > 0 {
		src = headers[len(headers)-1][1]
	}
	if src != "" {
		s.JSessionID = cookieVal(src, "JSESSIONID")
		s.ValidationKey = cookieVal(src, "validationKey")
		s.PLC = cookieVal(src, "PLC")
	}
	if s.ValidationKey == "" {
		if m := vkRe.FindStringSubmatch(text); m != nil {
			s.ValidationKey = m[1]
		}
	}
	if m := msisdnRe.FindStringSubmatch(text); m != nil {
		s.Msisdn, _ = url.QueryUnescape(m[1])
	}
	return s
}
