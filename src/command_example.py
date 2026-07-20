import json
import os
import sys

import zmq


request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"action": "ping"}
context = zmq.Context.instance()
socket = context.socket(zmq.REQ)
socket.setsockopt(zmq.LINGER, 0)
socket.setsockopt(zmq.RCVTIMEO, 5000)
socket.connect(f"tcp://{os.getenv('CTP_PROXY_HOST', '127.0.0.1')}:{os.getenv('ZMQ_REP_PORT', '5566')}")
socket.send_json(request)
print(json.dumps(socket.recv_json(), ensure_ascii=False, indent=2))

