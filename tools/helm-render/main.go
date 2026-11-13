// Command helm-render renders a Helm chart's templates with the Go template engine Helm uses and
// the subset of Sprig functions this repository's chart relies on. It exists so the chart can be
// rendered and validated in environments without the helm binary (CI also runs real `helm lint`).
//
// Input (stdin): JSON {"Values": ..., "Chart": ..., "Release": ...}; values already merged.
// Argument: the chart directory. Output: the rendered manifests, Helm-style, with "# Source:".
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"text/template"
)

func empty(v any) bool {
	if v == nil {
		return true
	}
	rv := reflect.ValueOf(v)
	switch rv.Kind() {
	case reflect.String, reflect.Map, reflect.Slice, reflect.Array:
		return rv.Len() == 0
	case reflect.Bool:
		return !rv.Bool()
	}
	if n, ok := v.(json.Number); ok {
		f, _ := n.Float64()
		return f == 0
	}
	return false
}

var plain = func(s string) bool {
	if s == "" || strings.TrimSpace(s) != s {
		return false
	}
	switch strings.ToLower(s) {
	case "true", "false", "yes", "no", "on", "off", "null", "~", "y", "n":
		return false
	}
	if _, err := strconv.ParseFloat(s, 64); err == nil {
		return false
	}
	if strings.ContainsAny(s[:1], "!&*-?|>'\"%@`{}[],#:") || strings.Contains(s, ": ") ||
		strings.Contains(s, " #") || strings.ContainsAny(s, "\n\t") || strings.HasSuffix(s, ":") {
		return false
	}
	return true
}

func scalar(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case string:
		if plain(x) {
			return x
		}
		b, _ := json.Marshal(x)
		return string(b)
	case json.Number:
		return x.String()
	case bool:
		return strconv.FormatBool(x)
	}
	return fmt.Sprint(v)
}

func toYaml(v any, ind string, b *strings.Builder) {
	switch x := v.(type) {
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			val := x[k]
			b.WriteString(ind + scalar(k) + ":")
			if m, ok := val.(map[string]any); ok && len(m) > 0 {
				b.WriteString("\n")
				toYaml(m, ind+"  ", b)
			} else if l, ok := val.([]any); ok && len(l) > 0 {
				b.WriteString("\n")
				toYaml(l, ind, b)
			} else {
				b.WriteString(" " + inline(val) + "\n")
			}
		}
	case []any:
		for _, item := range x {
			if m, ok := item.(map[string]any); ok && len(m) > 0 {
				var sub strings.Builder
				toYaml(m, ind+"  ", &sub)
				s := sub.String()
				b.WriteString(ind + "- " + strings.TrimPrefix(s, ind+"  "))
			} else {
				b.WriteString(ind + "- " + inline(item) + "\n")
			}
		}
	default:
		b.WriteString(ind + inline(v) + "\n")
	}
}

func inline(v any) string {
	switch x := v.(type) {
	case map[string]any:
		if len(x) == 0 {
			return "{}"
		}
	case []any:
		if len(x) == 0 {
			return "[]"
		}
	}
	return scalar(v)
}

func toInt(v any) int {
	switch x := v.(type) {
	case json.Number:
		f, _ := x.Float64()
		return int(f)
	case int:
		return x
	case int64:
		return int(x)
	case float64:
		return int(x)
	case string:
		n, _ := strconv.Atoi(x)
		return n
	}
	return 0
}

func indent(n int, s string) string {
	pad := strings.Repeat(" ", n)
	return pad + strings.ReplaceAll(s, "\n", "\n"+pad)
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: helm-render CHART_DIR < context.json")
		os.Exit(2)
	}
	chartDir := os.Args[1]
	dec := json.NewDecoder(os.Stdin)
	dec.UseNumber()
	var ctx map[string]any
	if err := dec.Decode(&ctx); err != nil {
		fail(err)
	}
	chart, _ := ctx["Chart"].(map[string]any)
	name, _ := chart["Name"].(string)
	base := name + "/templates"
	ctx["Template"] = map[string]any{"BasePath": base}

	root := template.New("root")
	funcs := template.FuncMap{
		"default": func(d any, v ...any) any {
			if len(v) == 0 || empty(v[0]) {
				return d
			}
			return v[0]
		},
		"quote": func(v any) string {
			if v == nil {
				return `""`
			}
			return strconv.Quote(fmt.Sprint(v))
		},
		"required": func(msg string, v any) (any, error) {
			if empty(v) {
				return nil, errors.New(msg)
			}
			return v, nil
		},
		"trunc": func(n int, s string) string {
			if len(s) > n {
				return s[:n]
			}
			return s
		},
		"int": toInt,
		"add": func(vs ...any) int {
			t := 0
			for _, v := range vs {
				t += toInt(v)
			}
			return t
		},
		"list": func(vs ...any) []any { return vs },
		"has": func(needle any, l []any) bool {
			for _, v := range l {
				if reflect.DeepEqual(v, needle) {
					return true
				}
			}
			return false
		},
		"hasPrefix":  func(p, s string) bool { return strings.HasPrefix(s, p) },
		"fail":       func(msg string) (string, error) { return "", errors.New(msg) },
		"trimSuffix": func(suf, s string) string { return strings.TrimSuffix(s, suf) },
		"sha256sum": func(s string) string {
			h := sha256.Sum256([]byte(s))
			return hex.EncodeToString(h[:])
		},
		"toYaml": func(v any) string {
			var b strings.Builder
			toYaml(v, "", &b)
			return strings.TrimSuffix(b.String(), "\n")
		},
		"indent":  indent,
		"nindent": func(n int, s string) string { return "\n" + indent(n, s) },
		"include": func(string, any) (string, error) { return "", nil }, // replaced below
	}
	root.Funcs(funcs).Option("missingkey=default")
	root.Funcs(template.FuncMap{"include": func(tname string, data any) (string, error) {
		var buf bytes.Buffer
		err := root.ExecuteTemplate(&buf, tname, data)
		return buf.String(), err
	}})

	files, err := filepath.Glob(filepath.Join(chartDir, "templates", "*"))
	if err != nil {
		fail(err)
	}
	sort.Strings(files)
	var manifests []string
	for _, f := range files {
		src, err := os.ReadFile(f)
		if err != nil {
			fail(err)
		}
		tname := base + "/" + filepath.Base(f)
		if _, err := root.New(tname).Parse(string(src)); err != nil {
			fail(err)
		}
		if strings.HasSuffix(f, ".yaml") {
			manifests = append(manifests, tname)
		}
	}
	for _, tname := range manifests {
		var buf bytes.Buffer
		if err := root.ExecuteTemplate(&buf, tname, ctx); err != nil {
			fail(err)
		}
		out := strings.ReplaceAll(buf.String(), "<no value>", "")
		for _, doc := range strings.Split(out, "\n---") {
			if strings.TrimSpace(doc) == "" {
				continue
			}
			fmt.Printf("---\n# Source: %s\n%s\n", tname, strings.Trim(doc, "\n"))
		}
	}
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "helm-render:", err)
	os.Exit(1)
}
