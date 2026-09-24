package resourcesvc

import (
	"errors"
	"net/http"

	"fencingdemo/internal/apiutil"
)

// HTTPHandler 把资源服务暴露为 HTTP 接口：
//
//	PUT /v1/resources/{key}  {"value":"...","token":3}
//	GET /v1/resources/{key}
func (s *Service) HTTPHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /v1/resources/{key}", s.handleWrite)
	mux.HandleFunc("GET /v1/resources/{key}", s.handleRead)
	return mux
}

type writeRequest struct {
	Value string `json:"value"`
	Token uint64 `json:"token"`
}

type resourceResponse struct {
	Key   string `json:"key"`
	Value string `json:"value"`
	Token uint64 `json:"token"`
}

func (s *Service) handleWrite(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	var req writeRequest
	if err := apiutil.DecodeJSON(r, &req); err != nil {
		apiutil.WriteError(w, http.StatusBadRequest, err.Error())
		return
	}
	err := s.Write(key, req.Value, req.Token)
	switch {
	case err != nil && errors.Is(err, ErrStaleToken):
		cur, _ := s.Read(key)
		apiutil.WriteJSON(w, http.StatusConflict, map[string]any{
			"error": err.Error(),
			"seen":  cur.Token,
		})
	case err != nil:
		apiutil.WriteError(w, http.StatusBadRequest, err.Error())
	default:
		apiutil.WriteJSON(w, http.StatusOK, resourceResponse{Key: key, Value: req.Value, Token: req.Token})
	}
}

func (s *Service) handleRead(w http.ResponseWriter, r *http.Request) {
	key := r.PathValue("key")
	res, ok := s.Read(key)
	if !ok {
		apiutil.WriteError(w, http.StatusNotFound, "resource not found")
		return
	}
	apiutil.WriteJSON(w, http.StatusOK, resourceResponse{Key: key, Value: res.Value, Token: res.Token})
}
