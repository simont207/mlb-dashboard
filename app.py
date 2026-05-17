from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "<h1>⚾ MLB Dashboard</h1><p>Flask is working!</p>"

@app.route("/health")
def health():
    return "OK"

if __name__ == "__main__":
    app.run()
