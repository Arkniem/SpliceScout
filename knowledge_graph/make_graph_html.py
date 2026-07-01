#!/usr/bin/env python3
"""make_graph_html.py -- render knowledge_graph.json + internals.json into a single self-contained
interactive HTML explorer (no external dependencies, opens in any browser).

  graph.html shows:
    * the 74-file dependency graph (force layout, colored by component, hover to trace links)
    * click any file -> a panel lists EVERYTHING inside it: module summary + every function /
      method / class with its signature, one-line description, and what it calls
    * a search box that matches file names AND symbol names/descriptions across the whole project
    * the legend doubles as a per-subsystem filter

Run AFTER graphify.py:  python knowledge_graph/make_graph_html.py   ->  knowledge_graph/graph.html
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
g = json.load(open(os.path.join(HERE, "knowledge_graph.json"), encoding="utf-8"))
internals = json.load(open(os.path.join(HERE, "internals.json"), encoding="utf-8"))

nodes = []
for fid, f in g["files"].items():
    d = g["degree"][fid]
    nodes.append({"id": fid, "n": f["name"], "c": f["component"], "k": f["kind"],
                  "loc": f["loc"], "in": d["in"], "out": d["out"], "ns": len(f["symbols"])})
edges = [{"s": e["src"], "d": e["dst"], "t": e["type"]} for e in g["edges"]]
payload = {"nodes": nodes, "edges": edges, "internals": internals,
           "stats": {"files": g["file_count"], "symbols": g["symbol_count"],
                     "edges": g["edge_count"], "generated": g["generated"]}}

HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SpliceScout — code knowledge graph</title>
<style>
:root{--bg:#fbfbfa;--panel:#fff;--ink:#1d1d1b;--mut:#6b6b66;--line:#e6e5e0;--accent:#534ab7}
@media(prefers-color-scheme:dark){:root{--bg:#1a1a19;--panel:#232322;--ink:#ececea;--mut:#9b9b95;--line:#34332f;--accent:#afa9ec}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
h1{font-size:17px;font-weight:600;margin:0}.sub{color:var(--mut);font-size:12px}
header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:10}
#search{flex:1;min-width:200px;max-width:420px;padding:8px 11px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font-size:13px}
#legend{display:flex;gap:6px 12px;flex-wrap:wrap;font-size:12px;color:var(--mut)}
.lchip{display:flex;align-items:center;gap:5px;cursor:pointer;padding:2px 7px;border-radius:6px;border:1px solid transparent}
.lchip.on{border-color:var(--line);background:var(--panel)}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
main{display:flex;gap:0;height:calc(100vh - 62px)}
#left{flex:1;min-width:0;position:relative;overflow:hidden}
#g{width:100%;height:100%;display:block}
#panel{width:430px;flex-shrink:0;border-left:1px solid var(--line);overflow:auto;padding:16px 18px;background:var(--panel)}
@media(max-width:820px){main{flex-direction:column;height:auto}#left{height:62vh}#panel{width:auto;border-left:none;border-top:1px solid var(--line)}}
.hint{color:var(--mut);font-size:13px}
.fhead{font-size:15px;font-weight:600;word-break:break-all}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:6px;margin:0 5px 5px 0}
.summary{margin:8px 0 14px;padding:9px 11px;border-radius:8px;background:var(--bg);font-size:13px;color:var(--ink)}
.sym{padding:9px 0;border-top:1px solid var(--line)}
.sig{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;color:var(--accent);word-break:break-word}
.sig .kw{color:var(--mut)}
.doc{font-size:12.5px;color:var(--ink);margin:3px 0 0}
.calls{font-size:11.5px;color:var(--mut);margin:3px 0 0}
.calls b{font-weight:600;color:var(--mut)}
.kindtag{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-left:6px}
text{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.srow{padding:5px 7px;border-radius:6px;cursor:pointer;font-size:12.5px}.srow:hover{background:var(--bg)}
.srow .f{color:var(--mut);font-size:11px}
mark{background:#ffe08a;color:#000;border-radius:2px}
</style></head><body>
<header>
  <div><h1>SpliceScout — code knowledge graph</h1><div class="sub" id="stat"></div></div>
  <input id="search" placeholder="Search files or functions (e.g. build_groups, classify, watchdog)…" autocomplete="off">
  <div id="legend"></div>
</header>
<main>
  <div id="left"><svg id="g" viewBox="0 0 900 720" preserveAspectRatio="xMidYMid meet"></svg></div>
  <div id="panel"><div class="hint">Click any file in the graph to see what's inside it — every function, what it does, and what it calls. Or search above.</div></div>
</main>
<script>
const G=__DATA__;
const CC={app:'#7F77DD',cluster_template:'#EF9F27',star_template:'#378ADD',bed_template:'#1D9E75',psi_template:'#D4537E',altanalyze:'#E24B4A'};
const CL={app:'app · Python',cluster_template:'cluster · download',star_template:'star · align',bed_template:'bed · bam→bed',psi_template:'psi · AltAnalyze',altanalyze:'AltAnalyze · py2'};
const CORDER=['app','cluster_template','star_template','bed_template','altanalyze','psi_template'];
const AN={app:[235,360],cluster_template:[500,150],star_template:[760,300],bed_template:[680,560],altanalyze:[840,650],psi_template:[400,620]};
const W=900,H=720,SVGNS='http://www.w3.org/2000/svg';
const nm={};G.nodes.forEach(n=>{n.deg=n.in+n.out;nm[n.id]=n});
const nbr={};G.nodes.forEach(n=>nbr[n.id]=new Set());
const E=G.edges.filter(e=>nm[e.s]&&nm[e.d]);E.forEach(e=>{nbr[e.s].add(e.d);nbr[e.d].add(e.s)});
const maxd=Math.max.apply(0,G.nodes.map(n=>n.deg));
function rad(n){return 5+Math.sqrt(n.deg)*2.6}
let sd=7;function rnd(){sd=(sd*9301+49297)%233280;return sd/233280}
G.nodes.forEach(n=>{const a=AN[n.c];n.x=a[0]+(rnd()-.5)*90;n.y=a[1]+(rnd()-.5)*90});
for(let t=0;t<560;t++){const al=1-t/560;
 for(let a=0;a<G.nodes.length;a++)for(let b=a+1;b<G.nodes.length;b++){const p=G.nodes[a],q=G.nodes[b];let dx=p.x-q.x,dy=p.y-q.y,d2=dx*dx+dy*dy||.01,d=Math.sqrt(d2);let f=(p.c===q.c?3000:4600)/d2,ux=dx/d,uy=dy/d;p.x+=ux*f*al;p.y+=uy*f*al;q.x-=ux*f*al;q.y-=uy*f*al}
 E.forEach(e=>{const p=nm[e.s],q=nm[e.d];let dx=q.x-p.x,dy=q.y-p.y,d=Math.sqrt(dx*dx+dy*dy)||.01;let f=(d-64)*.02*al,ux=dx/d,uy=dy/d;p.x+=ux*f;p.y+=uy*f;q.x-=ux*f;q.y-=uy*f});
 const ce={},ct={};G.nodes.forEach(n=>{ce[n.c]=ce[n.c]||[0,0];ce[n.c][0]+=n.x;ce[n.c][1]+=n.y;ct[n.c]=(ct[n.c]||0)+1});
 G.nodes.forEach(n=>{const cx=ce[n.c][0]/ct[n.c],cy=ce[n.c][1]/ct[n.c];n.x+=(cx-n.x)*.035*al;n.y+=(cy-n.y)*.035*al;n.x+=(450-n.x)*.006*al*(1+n.deg/maxd);n.y+=(360-n.y)*.006*al*(1+n.deg/maxd);n.x=Math.max(30,Math.min(W-30,n.x));n.y=Math.max(30,Math.min(H-26,n.y))})}
const svg=document.getElementById('g'),gE=document.createElementNS(SVGNS,'g'),gN=document.createElementNS(SVGNS,'g');svg.appendChild(gE);svg.appendChild(gN);
const lines=[];E.forEach(e=>{const l=document.createElementNS(SVGNS,'line'),x=e.t==='xlang';l.setAttribute('x1',nm[e.s].x);l.setAttribute('y1',nm[e.s].y);l.setAttribute('x2',nm[e.d].x);l.setAttribute('y2',nm[e.d].y);l.setAttribute('stroke',x?'#E24B4A':'var(--mut)');l.setAttribute('stroke-opacity',x?.55:.16);l.setAttribute('stroke-width',x?1.4:.8);if(x)l.setAttribute('stroke-dasharray','3 3');l._e=e;gE.appendChild(l);lines.push(l)});
let filter=null,sel=null,hov=null;
function applyVis(){G.nodes.forEach(n=>{const pf=!filter||n.c===filter,ph=!hov||n.id===hov||nbr[hov].has(n.id);const on=pf&&ph;n._g.style.opacity=on?1:.08;const lab=on&&(n.deg>=9||n.id===hov||n.id===sel||(filter&&n.c===filter)||(hov&&nbr[hov].has(n.id)));n._t.style.opacity=lab?1:0;n._c.setAttribute('stroke-width',n.id===sel?3:(n.k==='py2'?2.2:1.3))});
 lines.forEach(l=>{const e=l._e,x=e.t==='xlang';const pf=!filter||nm[e.s].c===filter||nm[e.d].c===filter,ph=!hov||e.s===hov||e.d===hov;const on=pf&&ph,touch=hov&&(e.s===hov||e.d===hov);l.setAttribute('stroke-opacity',on?(touch?.95:(x?.55:.16)):.03);l.setAttribute('stroke',touch?CC[nm[e.s].c]:(x?'#E24B4A':'var(--mut)'));l.setAttribute('stroke-width',touch?2:(x?1.4:.8))})}
G.nodes.forEach(n=>{const gg=document.createElementNS(SVGNS,'g');gg.setAttribute('transform','translate('+n.x+','+n.y+')');gg.style.cursor='pointer';
 const c=document.createElementNS(SVGNS,'circle');c.setAttribute('r',rad(n));c.setAttribute('fill',CC[n.c]);c.setAttribute('stroke','var(--panel)');c.setAttribute('stroke-width',n.k==='py2'?2.2:1.3);if(n.k==='py2')c.setAttribute('stroke-dasharray','2 1.5');if(n.k==='sh')c.setAttribute('fill-opacity','.82');
 const t=document.createElementNS(SVGNS,'text');t.textContent=n.n;t.setAttribute('y',rad(n)+11);t.setAttribute('text-anchor','middle');t.setAttribute('font-size','11');t.setAttribute('fill','var(--ink)');t.setAttribute('stroke','var(--panel)');t.setAttribute('stroke-width','2.6');t.setAttribute('paint-order','stroke');t.style.opacity=n.deg>=9?1:0;
 gg.appendChild(c);gg.appendChild(t);gN.appendChild(gg);n._g=gg;n._c=c;n._t=t;
 gg.addEventListener('mouseenter',()=>{hov=n.id;applyVis()});
 gg.addEventListener('mouseleave',()=>{hov=null;applyVis()});
 gg.addEventListener('click',()=>{select(n.id)})});
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function symHTML(s){const sig=s.k==='c'?'<span class="kw">class </span>'+esc(s.n):esc(s.n)+'<span class="kw">'+esc(s.a||'()')+'</span>';
 const kind=s.k==='c'?'class':s.k==='m'?'method':'func';
 let h='<div class="sym"><div class="sig">'+sig+'<span class="kindtag">'+kind+'</span></div>';
 if(s.d)h+='<div class="doc">'+esc(s.d)+'</div>';
 if(s.c&&s.c.length)h+='<div class="calls"><b>calls</b> '+s.c.map(esc).join(', ')+'</div>';
 return h+'</div>'}
function select(id,symName){sel=id;applyVis();const n=nm[id],info=G.internals[id]||{symbols:[],summary:''};
 const used=E.filter(e=>e.d===id).map(e=>nm[e.s].n),uses=E.filter(e=>e.s===id).map(e=>nm[e.d].n);
 let h='<div class="fhead">'+esc(n.n)+'</div><div class="sub" style="word-break:break-all">'+esc(id)+'</div>';
 h+='<div style="margin:8px 0 4px"><span class="badge" style="background:'+CC[n.c]+'22;color:'+CC[n.c]+'">'+CL[n.c]+'</span>'
   +'<span class="badge" style="background:var(--bg);color:var(--mut)">'+n.k+'</span>'
   +'<span class="badge" style="background:var(--bg);color:var(--mut)">'+n.loc+' loc</span>'
   +'<span class="badge" style="background:var(--bg);color:var(--mut)">'+info.symbols.length+' symbols</span></div>';
 if(info.summary)h+='<div class="summary">'+esc(info.summary)+'</div>';
 if(uses.length)h+='<div class="calls" style="margin-bottom:4px"><b>depends on:</b> '+uses.filter((v,i,a)=>a.indexOf(v)===i).map(esc).join(', ')+'</div>';
 if(used.length)h+='<div class="calls" style="margin-bottom:8px"><b>used by:</b> '+used.filter((v,i,a)=>a.indexOf(v)===i).map(esc).join(', ')+'</div>';
 if(!info.symbols.length)h+='<div class="hint">No functions detected (config/data or thin wrapper).</div>';
 info.symbols.forEach(s=>{h+=symHTML(s)});
 const p=document.getElementById('panel');p.innerHTML=h;p.scrollTop=0;
 if(symName){const els=p.querySelectorAll('.sig');for(const el of els){if(el.textContent.indexOf(symName)===0){el.parentElement.scrollIntoView({block:'center'});el.parentElement.style.background='var(--bg)';break}}}}
const lg=document.getElementById('legend');CORDER.forEach(c=>{const s=document.createElement('div');s.className='lchip';s.innerHTML='<span class="dot" style="background:'+CC[c]+'"></span>'+CL[c];s.onclick=()=>{filter=filter===c?null:c;[...lg.children].forEach(x=>x.classList.remove('on'));if(filter)s.classList.add('on');applyVis()};lg.appendChild(s)});
document.getElementById('stat').textContent=G.stats.files+' files · '+G.stats.symbols+' functions/classes · '+G.stats.edges+' edges · generated '+G.stats.generated;
// search across files + symbols
const sb=document.getElementById('search');
sb.addEventListener('input',()=>{const q=sb.value.trim().toLowerCase();if(!q){applyVis();return}
 const res=[];G.nodes.forEach(n=>{if(n.n.toLowerCase().indexOf(q)>=0)res.push({id:n.id,sym:null,label:n.n,sub:n.id})});
 for(const id in G.internals){G.internals[id].symbols.forEach(s=>{if(res.length<60&&(s.n.toLowerCase().indexOf(q)>=0||(s.d&&s.d.toLowerCase().indexOf(q)>=0)))res.push({id:id,sym:s.n,label:s.n+s.a,sub:nm[id].n+'  ·  '+(s.d||'')})})}
 let h='<div class="sub" style="margin-bottom:6px">'+res.length+' match'+(res.length===1?'':'es')+'</div>';
 res.slice(0,60).forEach((r,i)=>{h+='<div class="srow" data-i="'+i+'">'+esc(r.label)+'<div class="f">'+esc(r.sub)+'</div></div>'});
 const p=document.getElementById('panel');p.innerHTML=h;p._res=res;
 p.querySelectorAll('.srow').forEach(row=>{row.onclick=()=>{const r=res[+row.dataset.i];select(r.id,r.sym)}})});
applyVis();
</script></body></html>"""

out = HTML.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
dest = os.path.join(HERE, "graph.html")
with open(dest, "w", encoding="utf-8") as f:
    f.write(out)
print("wrote %s  (%d KB, %d files / %d symbols)" %
      (dest.replace("\\", "/"), len(out) // 1024, payload["stats"]["files"], payload["stats"]["symbols"]))
