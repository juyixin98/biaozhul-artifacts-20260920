package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"

	"reprobuild/internal/builder"
)

func httpPostBuild(baseURL string, body any) *builder.Record {
	b, err := json.Marshal(body)
	if err != nil {
		fatal(err)
	}
	resp, err := http.Post(baseURL+"/api/v1/builds", "application/json", bytes.NewReader(b))
	if err != nil {
		fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		fatal(errHTTP{resp.StatusCode, string(data)})
	}
	var rec builder.Record
	if err := json.Unmarshal(data, &rec); err != nil {
		fatal(err)
	}
	return &rec
}

func httpGetArtifact(baseURL, id string) []byte {
	resp, err := http.Get(baseURL + "/api/v1/builds/" + id + "/artifact")
	if err != nil {
		fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		data, _ := io.ReadAll(resp.Body)
		fatal(errHTTP{resp.StatusCode, string(data)})
	}
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		fatal(err)
	}
	return b
}

type errHTTP struct {
	code int
	body string
}

func (e errHTTP) Error() string { return "http " + http.StatusText(e.code) + ": " + e.body }
