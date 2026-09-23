// Package runner 在工作目录中执行节点命令。
// 安全要点：
//   - 工作目录固定为构建工作目录，命令中的相对路径无法逃逸；
//   - 环境变量只包含工具声明继承的白名单变量 + 工具固定变量；
//   - 支持每节点超时。
package runner

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"sort"
	"strings"
	"text/template"
	"time"

	"cdbg/internal/graph"
)

const (
	defaultTimeout = 10 * time.Minute
	maxOutputBytes = 1 << 20 // stdout/stderr 各最多保留 1 MiB
)

// Result 是一次命令执行的结果。
type Result struct {
	ExitCode int    `json:"exit_code"`
	Stdout   string `json:"stdout,omitempty"`
	Stderr   string `json:"stderr,omitempty"`
	Duration string `json:"duration"`
	TimedOut bool   `json:"timed_out,omitempty"`
}

// Runner 执行工具命令。
type Runner struct{}

func New() *Runner { return &Runner{} }

func newTemplate(nodeName, text string) (*template.Template, error) {
	return template.New(nodeName).Option("missingkey=error").Parse(text)
}

// RenderArgs 用 params 渲染 {{.KEY}} 模板参数。
func RenderArgs(tool *graph.Tool, node *graph.Node) ([]string, error) {
	out := make([]string, 0, len(node.Args))
	for i, a := range node.Args {
		tpl, err := newTemplate(node.Name, a)
		if err != nil {
			return nil, fmt.Errorf("node %q arg %d: %w", node.Name, i, err)
		}
		var sb strings.Builder
		if err := tpl.Execute(&sb, node.Params); err != nil {
			return nil, fmt.Errorf("node %q arg %d: %w", node.Name, i, err)
		}
		out = append(out, sb.String())
	}
	return out, nil
}

// Run 在 workdir 中执行 node 使用 tool 的命令。
func (r *Runner) Run(workdir string, tool *graph.Tool, node *graph.Node, inherited map[string]string) (*Result, error) {
	args, err := RenderArgs(tool, node)
	if err != nil {
		return nil, err
	}
	timeout := time.Duration(node.TimeoutSec) * time.Second
	if timeout <= 0 {
		timeout = defaultTimeout
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	var cmd *exec.Cmd
	if tool.Shell {
		// Shell 模式：command[0] 为整段 shell 脚本，渲染后的 args 以 $1.. 传入。
		shellArgs := append([]string{"-c", tool.Command[0], tool.Name}, args...)
		cmd = exec.CommandContext(ctx, "sh", shellArgs...)
	} else {
		effective := append(append([]string{}, tool.Command...), args...)
		cmd = exec.CommandContext(ctx, effective[0], effective[1:]...)
	}
	cmd.Dir = workdir
	cmd.Env = buildEnv(tool, inherited)

	var stdout, stderr cappedBuffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	start := time.Now()
	runErr := cmd.Run()
	res := &Result{
		Stdout:   stdout.String(),
		Stderr:   stderr.String(),
		Duration: time.Since(start).Round(time.Millisecond).String(),
	}

	if ctx.Err() == context.DeadlineExceeded {
		res.TimedOut = true
		res.ExitCode = -1
		return res, nil
	}
	if runErr != nil {
		var exitErr *exec.ExitError
		if ok := asExitError(runErr, &exitErr); ok {
			res.ExitCode = exitErr.ExitCode()
			return res, nil
		}
		return nil, fmt.Errorf("failed to start command for tool %q: %w", tool.Name, runErr)
	}
	res.ExitCode = 0
	return res, nil
}

func buildEnv(tool *graph.Tool, inherited map[string]string) []string {
	env := map[string]string{}
	keys := append([]string{}, tool.DeclaredEnv...)
	sort.Strings(keys)
	for _, k := range keys {
		if v, ok := inherited[k]; ok {
			env[k] = v
		}
	}
	for k, v := range tool.Env {
		env[k] = v
	}
	out := make([]string, 0, len(env))
	for k := range env {
		out = append(out, k+"="+env[k])
	}
	sort.Strings(out)
	return out
}

func asExitError(err error, target **exec.ExitError) bool {
	if e, ok := err.(*exec.ExitError); ok {
		*target = e
		return true
	}
	return false
}

type cappedBuffer struct {
	bytes.Buffer
	truncated bool
}

func (c *cappedBuffer) Write(p []byte) (int, error) {
	remaining := maxOutputBytes - c.Len()
	if remaining <= 0 {
		c.truncated = true
		return len(p), nil
	}
	if len(p) > remaining {
		c.truncated = true
		p = p[:remaining]
	}
	return c.Buffer.Write(p)
}

func (c *cappedBuffer) String() string {
	s := c.Buffer.String()
	if c.truncated {
		s += "\n...[output truncated]..."
	}
	return s
}
