"""ローカルWebサーバーの起動。"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def main() -> int:
    parser = argparse.ArgumentParser(description="setlogをPCのブラウザから使うローカルWebアプリ")
    parser.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス（既定はローカルのみ）")
    parser.add_argument("--port", type=int, default=8765, help="待ち受けポート（既定8765）")
    parser.add_argument("--open", action="store_true", help="起動後に既定のブラウザで開く")
    arguments = parser.parse_args()

    if sys.platform != "win32":
        print("このアプリはWindowsのDPAPIで認証情報を扱うため、Windows専用です。", file=sys.stderr)
        return 1

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicornが見つかりません。setlog-mcpをインストールした環境で実行してください。", file=sys.stderr
        )
        return 1

    from .app import app

    url = f"http://{arguments.host}:{arguments.port}/"
    if arguments.open:
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
    print(f"setlog PC Web: {url}", flush=True)
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
