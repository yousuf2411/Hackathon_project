#!/usr/bin/env python3
"""Who Stays On? Offline single-file API and web app."""
import json, sys, os, itertools, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent

class InputError(Exception): pass

def validate(data):
    if not isinstance(data, dict): raise InputError('Input must be a JSON object.')
    required = ['grid_available','generator_capacity_kw','fuel_budget_litres','fuel_table','max_consecutive_dark','shops']
    for k in required:
        if k not in data: raise InputError(f'Missing required field: {k}')
    grid=data['grid_available']; shops=data['shops']; table=data['fuel_table']
    if not isinstance(grid,list) or len(grid)!=10 or any(type(x) is not bool for x in grid): raise InputError('grid_available must contain exactly 10 booleans.')
    def number(v): return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0
    if not number(data['generator_capacity_kw']) or int(data['generator_capacity_kw']) != data['generator_capacity_kw'] or data['generator_capacity_kw']<=0: raise InputError('generator_capacity_kw must be a positive whole number.')
    if not number(data['fuel_budget_litres']) or round(data['fuel_budget_litres']*100)!=data['fuel_budget_litres']*100: raise InputError('fuel_budget_litres must be non-negative with at most two decimals.')
    if type(data['max_consecutive_dark']) is not int or data['max_consecutive_dark']<0: raise InputError('max_consecutive_dark must be a non-negative integer.')
    if not isinstance(table,list) or not table: raise InputError('fuel_table must be a non-empty list.')
    prev=0
    for row in table:
        if not isinstance(row,dict) or 'load_kw' not in row or 'litres_per_hour' not in row: raise InputError('Each fuel_table row needs load_kw and litres_per_hour.')
        if type(row['load_kw']) is not int or row['load_kw']<=prev or not number(row['litres_per_hour']) or round(row['litres_per_hour']*100)!=row['litres_per_hour']*100: raise InputError('fuel_table load levels must increase; fuel values must have at most two decimals.')
        prev=row['load_kw']
    if prev != data['generator_capacity_kw']: raise InputError('The last fuel_table load level must equal generator_capacity_kw.')
    if not isinstance(shops,list) or len(shops)>12: raise InputError('shops must be a list of at most 12 shops.')
    ids=set()
    for i,s in enumerate(shops):
        if not isinstance(s,dict): raise InputError(f'shops[{i}] must be an object.')
        for k in ['id','kw','type','value_per_slot']:
            if k not in s: raise InputError(f'shops[{i}] missing field: {k}.')
        if not isinstance(s['id'],str) or not s['id'].strip() or s['id'] in ids: raise InputError('Shop ids must be unique non-empty strings.')
        ids.add(s['id'])
        if type(s['kw']) is not int or s['kw']<=0: raise InputError(f'{s["id"]}: kw must be a positive whole number.')
        if s['type'] not in ('mandatory','normal'): raise InputError(f'{s["id"]}: type must be mandatory or normal.')
        vals=s['value_per_slot']
        if not isinstance(vals,list) or len(vals)!=10 or any(not number(v) or int(v)!=v for v in vals): raise InputError(f'{s["id"]}: value_per_slot must contain 10 non-negative whole numbers.')
    return data

def fuel_for(load, table):
    if load == 0: return 0.0
    for row in table:
        if load <= row['load_kw']: return float(row['litres_per_hour'])
    return None

def solve(raw, beam_width=1800):
    data=validate(raw); shops=data['shops']; grid=data['grid_available']; cap=data['generator_capacity_kw']; budget=round(data['fuel_budget_litres']*100); maxdark=data['max_consecutive_dark']; table=data['fuel_table']
    n=len(shops); outage=[i for i,g in enumerate(grid) if not g]
    if not outage:
        return {'status':'ok','roster':[],'value_total':0,'fuel_total_litres':0.0,'dark_hours':{s['id']:0 for s in shops},'message':'No outage hours; all shops are powered by the grid.'}
    mandatory=[i for i,s in enumerate(shops) if s['type']=='mandatory']
    if any(shops[i]['kw']>cap for i in mandatory):
        bad=[shops[i]['id'] for i in mandatory if shops[i]['kw']>cap]
        return {'status':'infeasible','roster':[],'value_total':0,'fuel_total_litres':0.0,'dark_hours':{s['id']:0 for s in shops},'message':f'Mandatory shop(s) exceed generator capacity: {", ".join(bad)}.'}
    # A state carries dark-run lengths, accumulated value/fuel, and chosen masks. Beam search ranks by value with a small fuel preference.
    # Precompute all feasible powered subsets for each outage hour.
    options=[]
    for h in outage:
        opts=[]
        for mask in range(1<<n):
            if any(not (mask>>i)&1 for i in mandatory): continue
            load=sum(shops[i]['kw'] for i in range(n) if (mask>>i)&1)
            if load>cap: continue
            f=fuel_for(load,table)
            if f is None: continue
            val=sum(shops[i]['value_per_slot'][h] for i in range(n) if (mask>>i)&1)
            opts.append((mask,load,round(f*100),val))
        opts.sort(key=lambda x:(x[3],-x[2]),reverse=True)
        options.append(opts)
    # Each state = (score, fuel_cents, dark_runs_tuple, masks_tuple, values tuple)
    states=[(0,0,tuple(0 for _ in shops),tuple(),tuple())]
    for pos,h in enumerate(outage):
        if pos > 0 and h > outage[pos-1] + 1:
            states=[(sc,fu,tuple(0 for _ in shops),ma,va) for sc,fu,ru,ma,va in states]
        next_states=[]
        for score,used,runs,masks,vals in states:
            for mask,load,fc,val in options[pos]:
                nf=used+fc
                if nf>budget: continue
                nr=[]; okay=True
                for i in range(n):
                    if (mask>>i)&1: nr.append(0)
                    else:
                        r=runs[i]+1
                        if r>maxdark: okay=False; break
                        nr.append(r)
                if not okay: continue
                next_states.append((score+val,nf,tuple(nr),masks+(mask,),vals+(val,)))
        if not next_states:
            return {'status':'infeasible','roster':[],'value_total':0,'fuel_total_litres':0.0,'dark_hours':{s['id']:0 for s in shops},'message':f'No roster satisfies mandatory, capacity, fuel budget and dark-run rules through outage hour {h+10}:00.'}
        # Deduplicate by runs and fuel; retain best value plan per state key.
        next_states.sort(key=lambda st:(st[0],-st[1]),reverse=True)
        seen=set(); trimmed=[]
        for st in next_states:
            key=(st[1],st[2])
            if key in seen: continue
            seen.add(key); trimmed.append(st)
            if len(trimmed)>=beam_width: break
        states=trimmed
    best=max(states,key=lambda st:(st[0],-st[1]))
    score,used,runs,masks,vals=best
    roster=[]; dark={s['id']:0 for s in shops}
    for pos,h in enumerate(outage):
        mask=masks[pos]; powered=[shops[i]['id'] for i in range(n) if (mask>>i)&1]; off=[shops[i]['id'] for i in range(n) if not ((mask>>i)&1)]
        for sid in off: dark[sid]+=1
        load=sum(shops[i]['kw'] for i in range(n) if (mask>>i)&1); litres=used # don't use cumulative; below from mask
        litres=fuel_for(load,table)
        reasons=[]
        for sid in off:
            i=next(i for i,s in enumerate(shops) if s['id']==sid)
            if shops[i]['type']=='mandatory': reason='Mandatory shop must remain powered.'
            else:
                reasons.append({'shop_id':sid,'reason':f'Kept off at {h+10:02d}:00 to respect the combined {load} kW load/capacity and preserve fuel for higher-value slots; value {shops[i]["value_per_slot"][h]}.'})
        roster.append({'hour':f'{h+10:02d}:00','slot':h,'powered_ids':powered,'dark_ids':off,'load_kw':load,'fuel_litres':round(litres,2),'value':vals[pos],'dark_reasons':reasons})
    return {'status':'ok','roster':roster,'value_total':score,'fuel_total_litres':round(used/100,2),'dark_hours':dark,'message':'High-value feasible roster found. Grid-on hours are not listed because every shop is powered by the grid.'}

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, typ='application/json; charset=utf-8'):
        b=body if isinstance(body,bytes) else body.encode('utf-8')
        self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        path=urlparse(self.path).path
        if path in ('/','/index.html'):
            try: self._send(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
            except Exception: self._send(500,'Web screen missing.','text/plain')
        elif path=='/api/health': self._send(200,json.dumps({'status':'ok','service':'Who Stays On API'}))
        elif path=='/sample.json': self._send(200,(ROOT/'sample.json').read_bytes())
        elif path=='/minute150.json': self._send(200,(ROOT/'minute150.json').read_bytes())
        else: self._send(404,json.dumps({'status':'not_found','message':'Not found'}))
    def do_POST(self):
        if urlparse(self.path).path!='/api/roster': self._send(404,json.dumps({'status':'not_found'})); return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size>2_000_000: raise InputError('Input is too large (maximum 2 MB).')
            data=json.loads(self.rfile.read(size).decode('utf-8'))
            result=solve(data)
            self._send(200 if result['status'] in ('ok','infeasible') else 400,json.dumps(result,ensure_ascii=False))
        except (json.JSONDecodeError,UnicodeDecodeError) as e: self._send(400,json.dumps({'status':'invalid_input','message':'Invalid JSON. Please check commas, quotes and brackets.'}))
        except InputError as e: self._send(400,json.dumps({'status':'invalid_input','message':str(e)}))
        except Exception as e: self._send(500,json.dumps({'status':'error','message':'Internal error while building roster.'}))
    def log_message(self, fmt, *args): print('%s - %s' % (self.address_string(), fmt%args))

if __name__=='__main__':
    port=int(os.environ.get('PORT','8000'))
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Who Stays On? running at http://127.0.0.1:{port}  (offline; Ctrl+C to stop)')
    server.serve_forever()
