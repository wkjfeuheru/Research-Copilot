"""Throwaway UI prototype. Run: python doc/prototype/serve.py"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __name__ == '__main__':
    root = Path(__file__).parent
    print('原型地址：http://127.0.0.1:8765/?variant=B&view=new', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8765), partial(SimpleHTTPRequestHandler, directory=str(root))).serve_forever()
