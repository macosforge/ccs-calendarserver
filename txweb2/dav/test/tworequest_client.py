from __future__ import print_function
import socket
import sys

test_type = sys.argv[1]
port = int(sys.argv[2])
socket_type = sys.argv[3]

s = socket.socket(socket.AF_INET)
s.connect(("127.0.0.1", port))
s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 40000)

if socket_type == 'ssl':
    s2 = socket.ssl(s)
    send = s2.write
    recv = s2.read
else:
    send = s.send
    recv = s.recv

print(">> Making %s request to port %d" % (socket_type, port), file=sys.stderr)

send("PUT /forbidden HTTP/1.1\r\n")
send("Host: localhost\r\n")

print(">> Sending lots of data", file=sys.stderr)
send("Content-Length: 100\r\n\r\n")
send("X" * 100)

send("PUT /forbidden HTTP/1.1\r\n")
send("Host: localhost\r\n")

print(">> Sending lots of data", file=sys.stderr)
send("Content-Length: 100\r\n\r\n")
send("X" * 100)

# import time
# time.sleep(5)
print(">> Getting data", file=sys.stderr)
data = ''
while len(data) < 299999:
    try:
        x = recv(10000)
    except:
        break
    if x == '':
        break
    data += x
sys.stdout.write(data)
