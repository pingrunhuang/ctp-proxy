import json
import os

import zmq


context = zmq.Context.instance()
subscriber = context.socket(zmq.SUB)
subscriber.connect(f"tcp://{os.getenv('CTP_PROXY_HOST', '127.0.0.1')}:{os.getenv('ZMQ_PUB_PORT', '5565')}")
subscriber.setsockopt_string(zmq.SUBSCRIBE, "marketdata.CTP.")

while True:
    topic, raw = subscriber.recv_multipart()
    print(topic.decode(), json.loads(raw))

