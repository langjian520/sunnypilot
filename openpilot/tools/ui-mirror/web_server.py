#!/usr/bin/env python3
"""openpilot UI 镜像 —— 网页服务端

两条通道，页面自己挑：

  MJPEG（默认，低延迟）
    ffmpeg 把每帧编成独立 JPEG，走本地 TCP 127.0.0.1:8554 送过来，
    这里连上去读、拆帧，再以 multipart/x-mixed-replace 转发给浏览器。
    每帧独立，没有 GOP、没有切片、没有播放缓冲 —— 端到端 ~0.3 秒。

  HLS（兼容回退）
    若改回 MIRROR_MODE=hls，ffmpeg 会在 /tmp/ui_mirror 切好切片，
    这里当静态文件发出去，VLC / Safari 都能直接放，代价是 ~3 秒延迟。

对外的地址：

  http://<设备IP>:8000/              自带播放器的页面，车机浏览器打开就能看
  http://<设备IP>:8000/stream.mjpeg  裸 MJPEG 流，VLC「打开网络串流」也能用
  http://<设备IP>:8000/live.m3u8     HLS 播放列表（仅 hls 模式）

只依赖 Python 标准库，不需要装任何第三方包。
"""

import http.server
import os
import posixpath
import socket
import sys
import threading
import time

# 环境变量都可以在 systemd unit 里覆盖
HLS_DIR = os.environ.get("UI_MIRROR_HLS_DIR", "/tmp/ui_mirror")
WEB_DIR = os.environ.get("UI_MIRROR_WEB_DIR", "/data/ui-mirror/web")
PORT = int(os.environ.get("UI_MIRROR_PORT", "8000"))
TCP_ADDR = os.environ.get("UI_MIRROR_TCP", "127.0.0.1:8554")
BOUNDARY = "uimirrorframe"

MIME = {
  ".m3u8": "application/vnd.apple.mpegurl",
  ".ts": "video/mp2t",
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".ico": "image/x-icon",
}

# 这些后缀每次都要重新取，绝不能缓存，否则画面会卡在旧内容上
NO_CACHE_EXT = (".m3u8", ".ts", ".html")

# JPEG 的起止标记。ffmpeg 的 mpjpeg 流就是一堆「multipart 头 + 裸 JPEG」，
# 按这两个标记切比解析 multipart 头更省事，也更抗丢包。
SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


class FrameHub:
  """从 ffmpeg 的 mpjpeg 流里持续读帧，永远只留最新的一帧。

  刻意只保留最新帧：慢的客户端自动跳到最新画面，延迟不会越积越多。
  多个浏览器标签页共享同一个上游连接，也省带宽。
  """

  def __init__(self, addr):
    host, _, port = addr.partition(":")
    self._addr = (host or "127.0.0.1", int(port or 8554))
    self._cond = threading.Condition()
    self._frame = None
    self._seq = 0
    self._connected = False
    threading.Thread(target=self._pump, daemon=True).start()

  @property
  def connected(self):
    with self._cond:
      return self._connected

  def wait_frame(self, last_seq, timeout=10.0):
    """等到比 last_seq 更新的一帧；超时就把当前这帧原样返回。"""
    with self._cond:
      if self._seq == last_seq:
        self._cond.wait(timeout)
      return self._seq, self._frame

  def _publish(self, frame):
    with self._cond:
      self._frame = frame
      self._seq += 1
      self._cond.notify_all()

  def _set_connected(self, flag):
    with self._cond:
      self._connected = flag
      self._cond.notify_all()

  def _pump(self):
    while True:
      try:
        sock = socket.create_connection(self._addr, timeout=3)
        sock.settimeout(10)
        self._set_connected(True)
        buf = b""
        while True:
          chunk = sock.recv(1 << 16)
          if not chunk:
            break
          buf += chunk
          while True:
            s = buf.find(SOI)
            if s < 0:
              # 手上这段是 multipart 头之类的非图像数据，丢掉；
              # 但末尾若是半个标记就先留着，下轮拼上
              buf = buf[-1:] if buf.endswith(b"\xff") else b""
              break
            e = buf.find(EOI, s + 2)
            if e < 0:
              buf = buf[s:]      # 这一帧还没收全，等下一块
              break
            self._publish(buf[s:e + 2])
            buf = buf[e + 2:]
          # 兜底：万一一直在垃圾数据里打转，别把内存吃满
          if len(buf) > (1 << 22):
            buf = buf[-65536:]
      except Exception:
        pass
      finally:
        self._set_connected(False)
        try:
          sock.close()
        except Exception:
          pass
      time.sleep(1)   # ffmpeg 还没起来 / 刚挂掉，等一会儿再连


class Handler(http.server.BaseHTTPRequestHandler):
  server_version = "ui-mirror/2.0"
  protocol_version = "HTTP/1.1"

  def log_message(self, *args):
    pass  # 安静点，别往 journal 里刷

  # ---------------------------------------------------------------- 路由

  def _resolve(self):
    """把 URL 映射成磁盘文件。只认白名单名字，天然防目录穿越。"""
    path = self.path.split("?", 1)[0].split("#", 1)[0]
    name = posixpath.basename(path)

    if path in ("", "/", "/index.html"):
      return os.path.join(WEB_DIR, "index.html")
    if name == "hls.min.js":
      return os.path.join(WEB_DIR, "hls.min.js")
    if name.endswith(".m3u8"):
      return os.path.join(HLS_DIR, name)
    if name.endswith(".ts"):
      return os.path.join(HLS_DIR, name)
    return None

  def _respond(self, fp, send_body=True):
    if fp is None:
      self.send_error(404, "not found")
      return

    try:
      size = os.path.getsize(fp)
      fh = open(fp, "rb")
    except OSError:
      # 开关还没打开时播放列表/片段还不存在，给个明确的 404
      self.send_error(404, "stream not ready (mirror toggle off?)")
      return

    with fh:
      ext = os.path.splitext(fp)[1].lower()
      self.send_response(200)
      self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
      self.send_header("Content-Length", str(size))
      self.send_header("Access-Control-Allow-Origin", "*")
      if ext in NO_CACHE_EXT:
        self.send_header("Cache-Control", "no-store, max-age=0")
      else:
        self.send_header("Cache-Control", "public, max-age=86400")
      self.end_headers()

      if not send_body:
        return

      try:
        while True:
          chunk = fh.read(64 * 1024)
          if not chunk:
            break
          self.wfile.write(chunk)
      except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # 播放器关了、切片段了，很正常，不用报错
        pass

  # ---------------------------------------------------------------- MJPEG

  def _stream_mjpeg(self):
    """把最新帧以 multipart/x-mixed-replace 推给浏览器，<img> 直接就能显示。"""
    # 先等到第一帧再发响应头：流还没起来时浏览器是「正在加载」，
    # 而不是一个坏掉的图片图标。开关打开后 1 秒内这里就会通过。
    seq, frame = HUB.wait_frame(-1, timeout=30.0)
    if frame is None:
      self.send_error(404, "stream not ready (mirror toggle off?)")
      return

    self.close_connection = True          # 无限长的流，没法用 keep-alive
    try:
      self.send_response(200)
      self.send_header("Content-Type",
                       "multipart/x-mixed-replace; boundary=" + BOUNDARY)
      self.send_header("Cache-Control", "no-store, max-age=0")
      self.send_header("Pragma", "no-cache")
      self.send_header("Connection", "close")
      self.end_headers()
    except (BrokenPipeError, ConnectionResetError):
      return

    try:
      while True:
        if frame is not None:
          head = ("--" + BOUNDARY + "\r\n"
                  "Content-Type: image/jpeg\r\n"
                  "Content-Length: " + str(len(frame)) + "\r\n\r\n").encode()
          self.wfile.write(head)
          self.wfile.write(frame)
          self.wfile.write(b"\r\n")
        # 有新帧立刻发；15 秒没动静就重发上一帧，让连接别因空闲被掐断
        seq, frame = HUB.wait_frame(seq, timeout=15.0)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
      # 浏览器关了页面，正常
      pass

  def do_GET(self):
    path = self.path.split("?", 1)[0]
    if path == "/stream.mjpeg":
      self._stream_mjpeg()
      return
    if path == "/health":
      body = ("mjpeg_source=%s connected=%s\n" % (TCP_ADDR, HUB.connected)).encode()
      self.send_response(200)
      self.send_header("Content-Type", "text/plain; charset=utf-8")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)
      return
    self._respond(self._resolve(), send_body=True)

  def do_HEAD(self):
    self._respond(self._resolve(), send_body=False)


class Server(http.server.ThreadingHTTPServer):
  daemon_threads = True
  allow_reuse_address = True


HUB = FrameHub(TCP_ADDR)


def main():
  srv = Server(("0.0.0.0", PORT), Handler)
  print("ui-mirror web on http://0.0.0.0:%d  mjpeg=%s  hls=%s  web=%s"
        % (PORT, TCP_ADDR, HLS_DIR, WEB_DIR), flush=True)
  try:
    srv.serve_forever()
  except KeyboardInterrupt:
    pass
  return 0


if __name__ == "__main__":
  sys.exit(main())
