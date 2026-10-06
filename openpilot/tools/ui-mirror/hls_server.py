#!/usr/bin/env python3
"""
把 ffmpeg 切好的 HLS 片段用 HTTP 发出去，让车机/手机浏览器直接播。

这是拿不到 mediamtx 时的备选方案：零额外依赖，openpilot 设备自带 python3 就能跑。

用法：
  python3 hls_server.py [目录] [端口]
  python3 hls_server.py /tmp/hls 8000
"""

import http.server
import socketserver
import sys

DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/hls"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000


class HLSHandler(http.server.SimpleHTTPRequestHandler):
  # 播放器很挑 MIME，显式声明
  extensions_map = {
    **http.server.SimpleHTTPRequestHandler.extensions_map,
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".mp4": "video/mp4",
  }

  def __init__(self, *args, **kwargs):
    super().__init__(*args, directory=DIR, **kwargs)

  def end_headers(self):
    self.send_header("Cache-Control", "no-store")
    self.send_header("Access-Control-Allow-Origin", "*")
    super().end_headers()

  def log_message(self, *args):
    pass  # 设备存储有限，别刷日志


class Server(socketserver.ThreadingTCPServer):
  allow_reuse_address = True
  daemon_threads = True


def main():
  with Server(("0.0.0.0", PORT), HLSHandler) as httpd:
    print(f"serving {DIR} on http://0.0.0.0:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
  main()
