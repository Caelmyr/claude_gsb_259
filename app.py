"""应用入口。

启动方式：python app.py  （默认监听 0.0.0.0:8000）
前端静态资源在 static/ 下，由 Flask 直接托管；/api/* 由 server.api 蓝图处理。
"""
from flask import Flask, send_from_directory
from flask_cors import CORS

from server import config
from server.api import init_app


def create_app():
    config.ensure_dirs()
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES
    app.config["JSON_AS_ASCII"] = False
    CORS(app)

    init_app(app)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    # threaded=True：批处理线程与请求线程互不阻塞
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
