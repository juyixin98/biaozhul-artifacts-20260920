import socket, json, time, urllib.request, sys

HOST, PORT, HPORT = "127.0.0.1", 2525, 8080
passed, failed = [], []

def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS" if cond else "FAIL"), "-", name, detail)

class SMTP:
    def __init__(self, port):
        self.c = socket.create_connection((HOST, port)); self.c.settimeout(3)
        self.greet = self._read()
    def _read(self):
        d=b""
        try:
            while True:
                k=self.c.recv(4096)
                if not k: break
                d+=k
                if d.endswith(b"\r\n") and not d.split(b"\r\n")[-2][3:4]==b"-":
                    # read until final (non-continuation) line
                    last=d.strip().split(b"\r\n")[-1]
                    if last[3:4]!=b"-": break
        except socket.timeout: pass
        return d.decode()
    def cmd(self, s): self.c.sendall(s.encode()); return self._read()
    def sendraw(self, b): self.c.sendall(b)
    def close(self): self.c.close()

def http(path, method="GET"):
    req=urllib.request.Request(f"http://{HOST}:{HPORT}{path}", method=method)
    try:
        with urllib.request.urlopen(req) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()

# --- Scenario 1: single-dot body + multiple recipients ---
s=SMTP(PORT)
check("1 greeting 220", s.greet.startswith("220"), s.greet.split()[0])
check("1 EHLO", s.cmd("EHLO acc.test\r\n").split("\r\n")[-2].startswith("250 "))
check("1 MAIL 250", s.cmd("MAIL FROM:<alice@acc.test>\r\n").startswith("250"))
r1=s.cmd("RCPT TO:<bob@acc.test>\r\n"); check("1 RCPT1 250", r1.startswith("250"))
r2=s.cmd("RCPT TO:<carol@acc.test>\r\n"); check("1 RCPT2 250", r2.startswith("250"))
check("1 DATA 354", s.cmd("DATA\r\n").startswith("354"))
# dot-stuffed: ".." => body ".", "...two" => body "..two"
s.sendraw(b"Subject: Acceptance 1\r\n\r\nline before\r\n..\r\n...two dots\r\nend\r\n.\r\n")
fin=s._read(); check("1 final 250 stored", fin.startswith("250"), fin.strip())
mid1=fin.split("stored as ")[-1].strip() if "stored as" in fin else ""
s.cmd("QUIT\r\n"); s.close()

# --- Scenario 2: out-of-order rejections ---
s=SMTP(PORT)
check("2 MAIL before EHLO 503", s.cmd("MAIL FROM:<x@y>\r\n").startswith("503"))
s.cmd("EHLO z\r\n")
check("2 RCPT before MAIL 503", s.cmd("RCPT TO:<y@z>\r\n").startswith("503"))
s.cmd("MAIL FROM:<a@b>\r\n")
check("2 DATA before RCPT 503", s.cmd("DATA\r\n").startswith("503"))
check("2 unknown 502", s.cmd("FROBNICATE\r\n").startswith("502"))
check("2 RSET 250", s.cmd("RSET\r\n").startswith("250"))
s.cmd("QUIT\r\n"); s.close()

# --- Scenario 3: oversize message (4KiB server on PORT 2526) ---
s=SMTP(2526)
s.cmd("EHLO x\r\n"); s.cmd("MAIL FROM:<big@test>\r\n"); s.cmd("RCPT TO:<b@test>\r\n")
s.cmd("DATA\r\n")
s.sendraw(b"x"*10240 + b"\r\n.\r\n")
ov=s._read(); check("3 oversize 552", ov.startswith("552"), ov.strip())
time.sleep(0.2); s.close()

# --- Scenario 4: interrupted DATA ---
s=SMTP(PORT)
s.cmd("EHLO x\r\n"); s.cmd("MAIL FROM:<drop@test>\r\n"); s.cmd("RCPT TO:<lost@test>\r\n")
s.cmd("DATA\r\n")
s.sendraw(b"never completed - no end dot")
s.c.close(); time.sleep(0.3)
check("4 interrupted DATA (connection closed, no response expected)", True)

# --- Verify via HTTP: exactly ONE message, the complete scenario-1 mail ---
time.sleep(0.3)
st, body = http("/messages")
data=json.loads(body)
check("HTTP 200", st==200, f"status={st}")
check("exactly 1 stored message", data["count"]==1, f"count={data['count']}")
if data["count"]==1:
    m=data["messages"][0]
    check("subject parsed", m["subject"]=="Acceptance 1", m["subject"])
    check("two recipients", m["to"]==["bob@acc.test","carol@acc.test"], str(m["to"]))
    check("sender", m["from"]=="alice@acc.test", m["from"])
    st2, raw = http(f"/messages/{m['id']}")
    rawtxt=raw.decode()
    check("dot line preserved", "\r\n.\r\n" in rawtxt)
    check("double-dot unstuffed", "\r\n..two dots\r\n" in rawtxt)
    check("CRLF framing", rawtxt.endswith("end\r\n"))

# oversize spool must be empty
import os
small=[f for f in os.listdir("/tmp/loopmail-demo/data-small/msgs")]
check("oversize spool empty", small==[], str(small))
# no orphan tmp files anywhere
tmp1=os.listdir("/tmp/loopmail-demo/data/tmp"); tmp2=os.listdir("/tmp/loopmail-demo/data-small/tmp")
check("no orphan temp files", tmp1==[] and tmp2==[], f"{tmp1} {tmp2}")

print(f"\n==== {len(passed)} passed, {len(failed)} failed ====")
if failed:
    print("FAILED:", failed); sys.exit(1)
