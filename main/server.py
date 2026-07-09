#!/usr/bin/env python3
"""
简单的 HTTP 服务器，用于本地查看 Global Trade Event Journal。

用法:
    python -m runs.server
    python -m runs.server --port 8080

然后在浏览器中打开:
    http://localhost:8080/journal_viewer.html
"""

import http.server
import socketserver
import argparse
from pathlib import Path

# 获取 runs 目录的绝对路径
RUNS_DIR = Path(__file__).parent.resolve()


class CustomHandler(http.server.SimpleHTTPRequestHandler):
    """自定义请求处理器"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(RUNS_DIR), **kwargs)

    def end_headers(self):
        # 添加 CORS 头允许跨域请求
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def log_message(self, format, *args):
        """自定义日志格式"""
        print(f"[{self.log_date_time_string()}] {format % args}")


def run_server(port=8080):
    """启动 HTTP 服务器"""
    print(f"\n📊 Global Trade Event Journal Viewer")
    print(f"=" * 50)
    print(f"📁 静态文件目录: {RUNS_DIR}")
    print(f"🌐 服务地址: http://localhost:{port}")
    print(f"")
    print(f"打开以下地址查看 Journal:")
    print(f"   http://localhost:{port}/journal_viewer.html")
    print(f"")
    print(f"Journal 文件: {RUNS_DIR / 'global_trade_events.json'}")
    print(f"")
    print(f"按 Ctrl+C 停止服务器")
    print(f"=" * 50)

    # 尝试使用 8080 端口，如果被占用自动切换
    for try_port in range(port, port + 10):
        try:
            with socketserver.TCPServer(("", try_port), CustomHandler) as httpd:
                print(f"\n✅ 服务器启动成功 on port {try_port}")
                httpd.serve_forever()
                break
        except OSError as e:
            if try_port == port + 9:
                print(f"❌ 无法启动服务器: {e}")
                raise
            print(f"端口 {try_port} 已被占用，尝试 {try_port + 1}...")
            continue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Global Trade Event Journal HTTP Server")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=8080,
        help="服务器端口 (默认: 8080)"
    )
    args = parser.parse_args()
    run_server(args.port)
