#!/usr/bin/env python3
"""openpilot UI 镜像 —— 网页服务端

把 ffmpeg 切好的 HLS 片段（/tmp/ui_mirror/live.m3u8 + live*.ts）通过 HTTP 发给车机：

  http://<设备IP>:8000/            自带播放器的页面，车机浏览器打开就能看
  http://<设备IP>:8000/live.m3u8   播放列表，给 VLC 之类的播放器用

只依赖 Python 标准库，不需要装任何第三方包。
"""

import http.server
import os
import posixpath
import sys

# 环境变量都可以在 systemd unit 里覆盖
HLS_DIR = os.environ.get("UI_MIRROR_HLS_DIR", "/tmp/ui_mirror")
WEB_DIR = os.environ.get("UI_MIRROR_WEB_DIR", "/data/ui-mirror/web")
PORT = int(os.environ.get("UI_MIRROR_PORT", "8000"))
PLAYLIST = os.environ.get("UI_MIRROR_PLAYLIST", "live.m3u8")

MIME = {
  ".m3u8": "application/vnd.apple.mpegurl",
  ".ts": "video/mp2t",
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".ico": "image/x-icon",
}

# 这些后缀每次都要重新取，绝不能缓存，否则画面会卡在旧片段上
NO_CACHE_EXT = (".m3u8", ".ts", ".html")


class Handler(http.server.BaseHTTPRequestHandler):
  server_version = "ui-mirror/1.0"
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

  def do_GET(self):
    self._respond(self._resolve(), send_body=True)

  def do_HEAD(self):
    self._respond(self._resolve(), send_body=False)


class Server(http.server.ThreadingHTTPServer):
  daemon_threads = True
  allow_reuse_address = True


def main():
  srv = Server(("0.0.0.0", PORT), Handler)
  print(f"ui-mirror web on http://0.0.0.0:{PORT}  hls={HLS_DIR}  web={WEB_DIR}", flush=True)
  try:
    srv.serve_forever()
  except KeyboardInterrupt:
    pass
  return 0


if __name__ == "__main__":
  sys.exit(main())
