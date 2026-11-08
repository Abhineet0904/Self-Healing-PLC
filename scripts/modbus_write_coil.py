#!/usr/bin/env python3
"""Write a single coil on a Modbus TCP simulator you own/control."""
import argparse, socket, struct, secrets
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--host',required=True,help='Tunnel host or lab server')
p.add_argument('--port',required=True,type=int,help='TCP port assigned by tunnel')
p.add_argument('--coil',type=int,default=0)
p.add_argument('--state',choices=['open','closed'],required=True,help='open = coil OFF; closed = coil ON')
a=p.parse_args()
tid=secrets.randbelow(65536); unit=1; value=0x0000 if a.state=='open' else 0xFF00
pdu=struct.pack('>BHH',5,a.coil,value)
frame=struct.pack('>HHHB',tid,0,len(pdu)+1,unit)+pdu
with socket.create_connection((a.host,a.port),timeout=5) as s:
    s.sendall(frame); response=s.recv(260)
if len(response)<12: raise SystemExit(f'No valid Modbus response: {response!r}')
r_tid,r_pid,r_len,r_unit=struct.unpack('>HHHB',response[:7])
if r_tid!=tid or response[7] != 5: raise SystemExit(f'Modbus exception or unexpected response: {response!r}')
print(f'OK: wrote coil {a.coil}={a.state} to {a.host}:{a.port}')
