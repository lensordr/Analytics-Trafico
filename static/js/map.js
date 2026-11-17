'use strict';
// Map: where every tractor + trailer is (last confirmed stop), where it unloads next, loaded trailers waiting,
// and "closest truck" for a new loading point. No GPS: positions come from confirmed stops.

/* ---------------------------------------------------------------- small slippy map (no external library) */
// Web-mercator tiles served by our own server (/tiles/z/x/y.png, HERE behind it) with HTML markers and SVG lines.
const TILE=256;
function mercX(lon,z){return (lon+180)/360*TILE*2**z;}
function mercY(lat,z){const s=Math.sin(Math.max(-85,Math.min(85,lat))*Math.PI/180);return (0.5-Math.log((1+s)/(1-s))/(4*Math.PI))*TILE*2**z;}
function unmerc(x,y,z){const n=TILE*2**z,lon=x/n*360-180,lat=Math.atan(Math.sinh(Math.PI*(1-2*y/n)))*180/Math.PI;return {lat,lon};}
class TileMap{
  constructor(el,view){this.el=el;this.z=view.z;this.center=view.center;this.layers={markers:[],lines:[]};
    el.innerHTML='<div class="tm-tiles"></div><svg class="tm-lines"></svg><div class="tm-markers"></div><div class="tm-zoom"><button type="button" data-tm="in" aria-label="Zoom in">+</button><button type="button" data-tm="out" aria-label="Zoom out">−</button></div><div class="tm-credit">Map © HERE</div>';
    this.tiles=el.querySelector('.tm-tiles');this.svg=el.querySelector('.tm-lines');this.marks=el.querySelector('.tm-markers');this.cache=new Map();
    el.querySelector('[data-tm=in]').onclick=()=>this.zoomAt(1);el.querySelector('[data-tm=out]').onclick=()=>this.zoomAt(-1);
    let drag=null;
    el.addEventListener('pointerdown',e=>{if(e.target.closest('.tm-zoom,.tm-marker'))return;drag={x:e.clientX,y:e.clientY,c:this.px(this.center),moved:false};el.setPointerCapture(e.pointerId);el.classList.add('dragging');});
    el.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)drag.moved=true;this.center=unmerc(drag.c.x-dx,drag.c.y-dy,this.z);this.draw();});
    const end=()=>{if(drag){drag=null;el.classList.remove('dragging');this.onmove?.();}};el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end);
    el.addEventListener('wheel',e=>{e.preventDefault();const r=el.getBoundingClientRect();this.zoomAt(e.deltaY<0?1:-1,e.clientX-r.left,e.clientY-r.top);},{passive:false});
    el.addEventListener('dblclick',e=>{if(e.target.closest('.tm-zoom,.tm-marker'))return;const r=el.getBoundingClientRect();this.zoomAt(1,e.clientX-r.left,e.clientY-r.top);});
    this.resize=new ResizeObserver(()=>this.draw());this.resize.observe(el);this.draw();}
  px(p,z=this.z){return {x:mercX(p.lon,z),y:mercY(p.lat,z)};}
  size(){return {w:this.el.clientWidth||800,h:this.el.clientHeight||500};}
  // Screen position (inside the map box) of a lat/lon.
  screen(p){const c=this.px(this.center),q=this.px(p),s=this.size();return {x:q.x-c.x+s.w/2,y:q.y-c.y+s.h/2};}
  zoomAt(step,sx,sy){const z=Math.max(3,Math.min(18,this.z+step));if(z===this.z)return;const s=this.size();sx??=s.w/2;sy??=s.h/2;
    const c=this.px(this.center),anchor=unmerc(c.x+sx-s.w/2,c.y+sy-s.h/2,this.z),a=this.px(anchor,z);this.z=z;this.center=unmerc(a.x-(sx-s.w/2),a.y-(sy-s.h/2),z);this.draw();this.onmove?.();}
  fit(points,maxZoom=11){const list=points.filter(Boolean);if(!list.length)return;const s=this.size();let z=maxZoom;
    for(;z>3;z--){const xs=list.map(p=>mercX(p.lon,z)),ys=list.map(p=>mercY(p.lat,z));if(Math.max(...xs)-Math.min(...xs)<s.w-90&&Math.max(...ys)-Math.min(...ys)<s.h-90)break;}
    const xs=list.map(p=>mercX(p.lon,z)),ys=list.map(p=>mercY(p.lat,z));this.z=z;this.center=unmerc((Math.max(...xs)+Math.min(...xs))/2,(Math.max(...ys)+Math.min(...ys))/2,z);this.draw();this.onmove?.();}
  set(markers,lines){this.layers={markers,lines};this.draw();}
  draw(){const s=this.size(),c=this.px(this.center),n=2**this.z,x0=Math.floor((c.x-s.w/2)/TILE),x1=Math.floor((c.x+s.w/2)/TILE),y0=Math.max(0,Math.floor((c.y-s.h/2)/TILE)),y1=Math.min(n-1,Math.floor((c.y+s.h/2)/TILE));
    const keep=new Set();
    for(let x=x0;x<=x1;x++)for(let y=y0;y<=y1;y++){const wx=((x%n)+n)%n,key=`${this.z}/${wx}/${y}`;keep.add(key);let img=this.cache.get(key);
      if(!img){img=new Image();img.alt='';img.className='tm-tile';img.src=`/tiles/${key}.png`;img.onerror=()=>img.classList.add('missing');this.cache.set(key,img);}
      img.style.transform=`translate(${Math.round(x*TILE-c.x+s.w/2)}px,${Math.round(y*TILE-c.y+s.h/2)}px)`;if(img.parentNode!==this.tiles)this.tiles.appendChild(img);}
    for(const [key,img] of this.cache)if(!keep.has(key)){img.remove();if(this.cache.size>400)this.cache.delete(key);}
    this.svg.setAttribute('width',s.w);this.svg.setAttribute('height',s.h);
    this.svg.innerHTML=this.layers.lines.map(l=>{const a=this.screen(l.from),b=this.screen(l.to);return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="tm-line ${l.cls||''}"/>`;}).join('');
    // Positions are set through the DOM: the page's security policy blocks inline style attributes.
    const shown=[];this.marks.innerHTML=this.layers.markers.map((m,i)=>{const p=this.screen(m.at);if(p.x<-150||p.y<-60||p.x>s.w+150||p.y>s.h+60)return '';shown.push(p);return `<button type="button" class="tm-marker ${m.cls||''}" data-action="map-marker" data-marker="${i}" title="${esc(m.title||'')}">${m.html}</button>`;}).join('');
    [...this.marks.children].forEach((el,i)=>{el.style.left=Math.round(shown[i].x)+'px';el.style.top=Math.round(shown[i].y)+'px';});}
}

/* ---------------------------------------------------------------- fleet positions */
let fleetMap=null,mapView={z:6,center:{lat:41.4,lon:2.0}},mapFitted=false,mapSelected=null,mapGroup=null;
const mapLayers={free:true,moving:true,waiting:true};
const mapSearch={loading_id:'',coords:null,at:'',results:null,exact:{}};
const pointOf=loc=>loc&&loc.lat!=null&&loc.lon!=null?{lat:+loc.lat,lon:+loc.lon}:null;
// Each active tractor: trailer, driver, where it is (last confirmed stop), where it goes next, and when/where it is free.
function fleetAssemblies(){const now=Date.now(),list=[];
  for(const t of state.trucks.filter(x=>x.active&&!x.archived_at)){
    const parts=state.orders.filter(o=>o.status!=='Cancelled').flatMap(o=>orderParts(o).filter(p=>p.truck_id===t.id).map(p=>({o,p,stops:partStops(o,p)})));
    // Where it is: the latest confirmed stop of its parts, else the reported fleet location.
    const done=parts.flatMap(x=>x.stops.filter(s=>s.status==='Done'&&s.done_at).map(s=>({s,o:x.o}))).sort((a,b)=>Date.parse(b.s.done_at)-Date.parse(a.s.done_at))[0];
    const here=done?getLocation(done.s.location_id):getLocation(t.location_id);
    // Where it goes: the next stop to confirm in its current (or next) part.
    const next=parts.filter(x=>!['Completed','Cancelled'].includes(x.o.status)).flatMap(x=>x.stops.filter(s=>s.status!=='Done').map(s=>({s,o:x.o,p:x.p}))).sort((a,b)=>Date.parse(a.s.planned_at)-Date.parse(b.s.planned_at))[0];
    const last=parts.sort((a,b)=>Date.parse(b.p.end)-Date.parse(a.p.end))[0];
    const busyUntil=last&&Date.parse(last.p.end)>now?last:null;
    const freeLoc=busyUntil?getLocation(orderStops(busyUntil.o).find(s=>s.seq===busyUntil.p.last)?.location_id||busyUntil.o.unloading_id):here;
    const driver=state.drivers.find(d=>d.id===(next?.p.driver_id||t.driver_id));
    const late=next&&stopIsLate(next.s);
    list.push({t,driver,trailer:t.trailer||'',here,next,late,status:late?'late':next?(next.o.status==='In transit'||next.s.seq>1?'moving':'planned'):'free',
      free_at:busyUntil?busyUntil.p.end:new Date(now).toISOString(),free_loc:freeLoc,own:/malcom/i.test(driver?.carrier||'')||!driver?.carrier});}
  return list;}
function mapWaitingTrailers(){const list=[];
  for(const o of state.orders.filter(x=>!['Cancelled','Completed'].includes(x.status)))for(const s of orderStops(o))if(s.kind==='Tractor change'&&!s.truck_id)list.push({trailer:o.trailer,loc:getLocation(s.location_id),to:getLocation(o.unloading_id),from:s.done_at||s.planned_at,order:o});
  for(const t of state.trailers||[]){if(!t.active||t.archived_at||!t.parked_location_id)continue;const cargo=parkedTrailerCargo(t.id);if(!cargo?.order)continue;
    if(state.orders.some(o=>o.status!=='Cancelled'&&o.trailer_id===t.id&&Date.parse(o.pickup_at)>=Date.parse(cargo.at)))continue;
    list.push({trailer:t.plate,trailer_id:t.id,loc:getLocation(t.parked_location_id),to:getLocation(cargo.unloading?.location_id),from:t.parked_at||cargo.at,order:cargo.order});}
  return list;}
const statusInfo={free:['Free','🟢'],planned:['Planned','🔵'],moving:['On the way','🔵'],late:['Late','🔴']};

/* ---------------------------------------------------------------- closest truck for a loading point */
const ROAD=1.25,KMH=65;
function mapTarget(){if(mapSearch.coords)return {...mapSearch.coords,label:`${mapSearch.coords.lat.toFixed(4)}, ${mapSearch.coords.lon.toFixed(4)}`};const loc=getLocation(mapSearch.loading_id),p=pointOf(loc);return p?{...p,label:placeLabel(loc),id:loc.id}:null;}
function airKm(a,b){const r=Math.PI/180,h=Math.sin((b.lat-a.lat)*r/2)**2+Math.cos(a.lat*r)*Math.cos(b.lat*r)*Math.sin((b.lon-a.lon)*r/2)**2;return 6371*2*Math.asin(Math.min(1,Math.sqrt(h)));}
function closestAssemblies(){const target=mapTarget();if(!target)return null;const wanted=mapSearch.at?Date.parse(mapSearch.at):Infinity;  // empty = as soon as possible
  return fleetAssemblies().map(a=>{const from=pointOf(a.free_loc);if(!from)return null;const exact=mapSearch.exact[a.t.id];const km=exact?exact.distance_km:airKm(from,target)*ROAD,hours=exact?exact.driving_hours:km/KMH;
    const ready=Math.max(Date.parse(a.free_at),Date.now()),arrive=ready+hours*3600000;return {...a,from,km,hours,exact:!!exact,ready,arrive,late_for:arrive>wanted};})
    .filter(Boolean).sort((x,y)=>(x.late_for-y.late_for)||x.arrive-y.arrive||x.km-y.km);}
async function exactKm(list){const target=mapTarget();if(!target)return;const todo=list.filter(r=>!mapSearch.exact[r.t.id]).slice(0,5);if(!todo.length)return;
  $('#map-exact-note')?.replaceChildren(document.createTextNode('Asking HERE for the truck road distance of the first 5…'));
  await Promise.all(todo.map(r=>api('/api/road-km',{truck_id:r.t.id,from_lat:r.from.lat,from_lon:r.from.lon,to_lat:target.lat,to_lon:target.lon}).then(v=>{mapSearch.exact[r.t.id]=v;}).catch(()=>{})));
  renderMapSide();drawFleetMap();}

/* ---------------------------------------------------------------- page */
function mapView_(){const fleet=fleetAssemblies(),waiting=mapWaitingTrailers(),placed=fleet.filter(a=>pointOf(a.here)||pointOf(a.next&&getLocation(a.next.s.location_id))),unplaced=fleet.length-placed.length;
  return heading('Map','Where every truck is, where it unloads next, and which truck is closest to a new loading.','new-order','New loading order')+`
  <div class="map-page"><section class="panel map-panel"><div class="map-toolbar"><div class="map-legend">${[['free','🟢 Free'],['moving','🔵 On the way'],['waiting','🟠 Loaded trailer waiting']].map(([k,l])=>`<label class="map-chip"><input type="checkbox" data-map-layer="${k}" ${mapLayers[k]?'checked':''}> ${l}</label>`).join('')}<span class="map-chip late-chip">🔴 Late</span></div><div class="map-find"><input id="map-find" type="search" placeholder="Find a truck or trailer… e.g. PH34" autocomplete="off" aria-label="Find a truck or trailer on the map"></div></div>
  <div id="fleet-map" class="fleet-map" aria-label="Fleet map"></div>
  <p class="map-foot">${placed.length} of ${fleet.length} trucks on the map · positions = last confirmed stop (no GPS)${unplaced?` · <strong>${unplaced}</strong> without a known position: confirm their stops or set a location on the tractor`:''}${waiting.length?` · ${waiting.length} loaded ${waiting.length===1?'trailer waits':'trailers wait'} for a tractor`:''}. Drag to move, scroll or +/− to zoom.</p></section>
  <aside class="map-side" id="map-side">${mapSideHtml()}</aside></div>`;}
function mapSideHtml(){const target=mapTarget(),results=target?closestAssemblies():null,sel=mapSelected;
  const selected=mapGroup?groupHtml(mapGroup):sel?selectedHtml(sel):'';
  return `${selected}<section class="panel map-search"><div class="panel-head"><div><h2>🔎 Closest truck for a loading</h2><p>1 · Choose where to load. 2 · Pick the time. The best trucks appear below and on the map.</p></div></div><div class="padded">
  ${locationPicker('1 · Loading point (saved place)','map_loading',mapSearch.loading_id,{empty:'Choose a saved place',placeholder:'Type a company or city…'})}
  <div class="field"><label for="map-coords">…or paste coordinates from Google Maps</label><input id="map-coords" placeholder="e.g. 37.4739, -4.4749" value="${mapSearch.coords?esc(mapSearch.coords.lat+', '+mapSearch.coords.lon):''}" autocomplete="off"></div>
  <div class="field"><label for="map-at">2 · When should it load? <span class="muted">(empty = as soon as possible)</span></label><input id="map-at" type="datetime-local" value="${esc(mapSearch.at||'')}"></div>
  ${!target?`<p class="subtitle">${mapSearch.loading_id?'This place has no coordinates yet: add them under Saved locations (paste from Google Maps).':'Choose a loading point to see the closest trucks.'}</p>`:''}
  </div>${results?`<div class="map-results"><p class="subtitle padded-x">Best first: ${mapSearch.at?`trucks that can be there by ${formatDate(dispatchStamp(Date.parse(mapSearch.at)))}, then `:''}the earliest arrival at the loading. <span id="map-exact-note">${Object.keys(mapSearch.exact).length?'✓ = road km from HERE; ≈ = estimate (straight line × 1.25, 65 km/h, no rests).':'≈ = estimate (straight line × 1.25, 65 km/h, no rests).'}</span></p>
  ${results.slice(0,12).map((r,i)=>resultCard(r,i)).join('')}${results.length?`<div class="padded-x"><button type="button" class="btn small" data-action="map-exact">✓ Exact road km for the first 5 (HERE)</button></div>`:empty('No truck with a known position','Confirm stops or set a location on the tractors.')}</div>`:''}</section>
  ${waitingListHtml()}`;}
function resultCard(r,i){const v=r.next?r.next:null;return `<article class="map-result ${r.late_for?'late':''}" data-map-truck="${r.t.id}"><div class="map-rank">${i+1}</div><div class="map-result-body"><h3>${esc(r.t.plate)}${r.trailer?` <span>· ${esc(r.trailer)}</span>`:''} ${r.own?'':badge('Subcontractor','amber')}</h3><p>${esc(r.driver?.name||'Driver not set')}</p>
  <p><strong>${Date.parse(r.free_at)>Date.now()?'Free '+formatDate(r.free_at):'Free now'}</strong> · ${esc(r.free_loc?.city||'?')}</p>
  <p>${r.exact?'✓':'≈'} <strong>${Math.round(r.km)} km</strong> empty · arrives ${r.exact?'':'≈ '}${formatDate(dispatchStamp(r.arrive))}${r.late_for?' · <span class="late-text">after the wanted time</span>':''}</p>
  <div class="actions"><button class="btn small primary" data-action="map-order" data-id="${r.t.id}">＋ Create order</button><button class="btn small" data-action="map-focus" data-id="${r.t.id}">Show on map</button></div></div></article>`;}
function waitingListHtml(){const list=mapWaitingTrailers();if(!list.length)return '';return `<section class="panel map-waiting"><div class="panel-head"><div><h2>🟠 Loaded trailers waiting <span class="section-count">${list.length}</span></h2><p>Left loaded; choose the tractor that takes each one on.</p></div></div>${list.map(w=>`<div class="map-waiting-row"><strong>${esc(w.trailer)}</strong><small>${esc(w.loc?.company||'?')} · since ${formatDate(w.from)}</small><small>→ ${esc(w.to?.company||'?')} · ${esc(w.to?.city||'')}</small>${w.trailer_id?`<button class="btn small" data-action="waiting-trailer-order" data-id="${w.trailer_id}">⛟ Create route</button>`:`<button class="btn small" data-action="order-detail" data-id="${w.order.id}">⛟ Choose tractor</button>`}</div>`).join('')}</section>`;}
function selectedHtml(id){const a=fleetAssemblies().find(x=>x.t.id===id);if(!a)return '';const n=a.next,dest=n&&getLocation(n.s.location_id);
  return `<section class="panel map-selected"><div class="panel-head"><div><h2>${esc(a.t.plate)}${a.trailer?' · '+esc(a.trailer):''} ${badge(statusInfo[a.status][0],a.status==='late'?'amber':a.status==='free'?'':'blue')}</h2><p>${esc(a.driver?.name||'Driver not set')}${a.driver?.phone?` · <a href="${esc(telHref(a.driver.phone))}">☎ ${esc(a.driver.phone)}</a>`:''}</p></div><button type="button" class="icon-button" data-action="map-unselect" aria-label="Close">×</button></div><div class="padded">
  <p><span class="muted">Last known place</span><br><strong>${esc(a.here?placeLabel(a.here):'Unknown')}</strong></p>
  ${n?`<p><span class="muted">Next: ${esc(stopKindLabel(n.o,n.s))}</span><br><strong>${esc(placeLabel(dest))}</strong><br>${formatDate(n.s.planned_at)}${a.late?' · <span class="late-text">overdue</span>':''}</p>`:'<p><strong>No journey planned.</strong></p>'}
  <p><span class="muted">Free</span><br><strong>${Date.parse(a.free_at)>Date.now()?formatDate(a.free_at):'Now'}</strong> · ${esc(a.free_loc?.city||'?')}</p>
  <div class="actions">${n?`<button class="btn small" data-action="order-detail" data-id="${n.o.id}">Open journey</button>`:''}${a.free_loc?`<button class="btn small primary" data-action="nearby" data-id="${a.free_loc.id}" data-truck="${a.t.id}" data-driver="${a.driver?.id||''}" data-from="${esc(a.free_at)}" data-context="${esc(`${a.t.plate} · free ${formatDate(a.free_at)}`)}">⇄ Next loads</button>`:''}</div></div></section>`;}
function renderMapSide(){const side=$('#map-side');if(side)side.innerHTML=mapSideHtml();}
// Markers: one per place (several trucks at the base become one marker), next unloading with a line, waiting trailers, the loading point.
function drawFleetMap(){const box=$('#fleet-map');if(!box)return;if(!fleetMap||fleetMap.el!==box){fleetMap=new TileMap(box,mapView);fleetMap.onmove=()=>{mapView={z:fleetMap.z,center:fleetMap.center};};}
  const fleet=fleetAssemblies(),markers=[],lines=[],groups=new Map(),q=searchNorm($('#map-find')?.value||'');
  const results=mapTarget()?closestAssemblies()?.slice(0,5):null,top=new Set((results||[]).map(r=>r.t.id));
  for(const a of fleet){const layer=a.status==='free'?'free':'moving';if(!mapLayers[layer]&&!top.has(a.t.id)&&mapSelected!==a.t.id)continue;const at=pointOf(a.here),dest=a.next&&getLocation(a.next.s.location_id),to=pointOf(dest);
    if(at){const key=a.here.id;if(!groups.has(key))groups.set(key,{at,loc:a.here,list:[]});groups.get(key).list.push(a);}
    if(to&&a.next&&mapLayers.moving){if(at)lines.push({from:at,to,cls:a.late?'late':''});markers.push({at:to,cls:`dest ${a.late?'late':''}`,html:`${stopIcon[a.next.s.kind]||'•'} ${esc(dest.city)} <small>${esc(a.t.plate)} · ${formatTime(a.next.s.planned_at)}</small>`,title:`${a.t.plate}: ${stopKindLabel(a.next.o,a.next.s)} ${placeLabel(dest)} ${formatDate(a.next.s.planned_at)}`,truck:a.t.id});}}
  for(const g of groups.values()){const one=g.list.length===1?g.list[0]:null,worst=g.list.some(a=>a.status==='late')?'late':g.list.some(a=>a.status!=='free')?'moving':'free',hit=q&&g.list.some(a=>searchNorm(a.t.plate+' '+a.trailer).includes(q));
    markers.push({at:g.at,cls:`truck ${worst} ${hit?'hit':''} ${g.list.some(a=>top.has(a.t.id))?'top':''} ${g.list.some(a=>a.t.id===mapSelected)?'selected':''}`,html:one?`${statusInfo[one.status][1]} <b>${esc(one.t.plate)}</b>${one.trailer?` <small>${esc(one.trailer)}</small>`:''}`:`${worst==='late'?'🔴':worst==='free'?'🟢':'🔵'} <b>${g.list.length} trucks</b> <small>${esc(g.loc.city)}</small>`,title:g.list.map(a=>`${a.t.plate} ${a.trailer} · ${statusInfo[a.status][0]}`).join('\n'),group:g.list.map(a=>a.t.id)});}
  if(mapLayers.waiting)for(const w of mapWaitingTrailers()){const at=pointOf(w.loc);if(!at)continue;markers.push({at,cls:'waiting',html:`🟠 <b>${esc(w.trailer)}</b> <small>loaded → ${esc(w.to?.city||'?')}</small>`,title:`${w.trailer} waits loaded at ${placeLabel(w.loc)}`,waiting:w});const to=pointOf(w.to);if(to)lines.push({from:at,to,cls:'waiting'});}
  const target=mapTarget();if(target){markers.push({at:target,cls:'target',html:`📍 <b>Loading</b> <small>${esc(target.label.split(' · ').slice(0,2).join(' · '))}</small>`,title:'Loading point'});for(const r of results||[])lines.push({from:r.from,to:target,cls:'candidate'});}
  fleetMap.set(markers,lines);fleetMap.markerData=markers;
  if(!mapFitted){const pts=markers.map(m=>m.at);if(pts.length){fleetMap.fit(pts,9);mapFitted=true;}}}
function focusTruck(id){mapGroup=null;const a=fleetAssemblies().find(x=>x.t.id===Number(id));const p=pointOf(a?.here)||pointOf(a?.free_loc);mapSelected=Number(id);renderMapSide();if(p&&fleetMap){fleetMap.z=Math.max(fleetMap.z,9);fleetMap.center=p;fleetMap.onmove?.();}drawFleetMap();}
// Several trucks at one place (e.g. the base): list them, pick one.
function groupHtml(ids){const fleet=fleetAssemblies().filter(a=>ids.includes(a.t.id)),place=fleet[0]?.here;
  return `<section class="panel map-selected"><div class="panel-head"><div><h2>${fleet.length} trucks · ${esc(place?.company||'')}</h2><p>${esc(place?.city||'')} · last confirmed stop here</p></div><button type="button" class="icon-button" data-action="map-unselect" aria-label="Close">×</button></div>${fleet.map(a=>`<div class="map-waiting-row"><strong>${statusInfo[a.status][1]} ${esc(a.t.plate)}${a.trailer?' · '+esc(a.trailer):''}</strong><small>${esc(a.driver?.name||'Driver not set')} · ${a.next?esc(stopKindLabel(a.next.o,a.next.s))+' '+esc(getLocation(a.next.s.location_id)?.city||'')+' '+formatDate(a.next.s.planned_at):'Free'}</small><button class="btn small" data-action="map-focus" data-id="${a.t.id}">Details</button></div>`).join('')}</section>`;}
function mapMarkerClick(index){const m=fleetMap?.markerData?.[Number(index)];if(!m)return;
  if(m.group?.length===1||m.truck){mapGroup=null;focusTruck(m.truck||m.group[0]);return;}
  if(m.group){mapGroup=m.group;mapSelected=null;renderMapSide();drawFleetMap();$('#map-side').scrollIntoView({block:'nearest'});return;}
  if(m.waiting){if(m.waiting.trailer_id)openOrder({trailer_id:m.waiting.trailer_id});else orderDetail(m.waiting.order.id);}}
