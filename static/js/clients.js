'use strict';
// ---------------------------------------------------------------- Clients with routes
// Only the companies that have saved routes as client (Tariffs → Client): their order-form link ready to copy or
// send, and in "Next loads" the clients to call around a free truck, with a ready WhatsApp offer.
let clientsQuery='',clientsSort='orders';
const CLIENT_OFFER={es:(place,when,url)=>`Hola, ¿qué tal? Tenemos un camión libre en ${place}${when?' '+when:''}. ¿Tenéis alguna carga? Podéis pedirla aquí: ${url}`,
  en:(place,when,url)=>`Hello! We have a truck free in ${place}${when?' '+when:''}. Do you have a load for us? You can request it here: ${url}`};
function clientsWithRoutes(){const map=new Map();
  for(const t of state.tariffs||[]){if(!t.active||!t.client_id)continue;const c=map.get(t.client_id)||{id:t.client_id,routes:0,from:new Set(),orders:0,last:''};c.routes++;c.from.add(t.from_id);map.set(t.client_id,c);}
  for(const o of state.orders){if(o.status==='Cancelled'||!map.has(o.client_id))continue;const c=map.get(o.client_id);c.orders++;if(o.pickup_at>c.last)c.last=o.pickup_at;}
  for(const r of state.load_requests||[]){const c=map.get(r.client_id);if(c)c.requests=(c.requests||0)+1;}
  return [...map.values()].map(c=>({...c,place:getLocation(c.id)})).filter(c=>c.place);}
function clientsView(){const words=searchNorm(clientsQuery).split(' ').filter(Boolean),all=clientsWithRoutes();
  const list=all.filter(c=>!words.length||words.every(w=>searchNorm(`${c.place.company} ${c.place.city} ${c.place.postal_code} ${c.place.phone||''}`).includes(w)))
    .sort({orders:(a,b)=>b.orders-a.orders||b.routes-a.routes,routes:(a,b)=>b.routes-a.routes,name:(a,b)=>a.place.company.localeCompare(b.place.company),recent:(a,b)=>String(b.last).localeCompare(String(a.last))}[clientsSort]);
  const sorts=[['orders','Most orders'],['routes','Most routes'],['recent','Last order'],['name','Name']].map(([k,l])=>`<button class="${clientsSort===k?'selected':''}" data-action="clients-sort" data-id="${k}">${l}</button>`).join('');
  return heading('Clients','Companies with saved routes as client. Copy their order-form link or send it on WhatsApp: they see only their routes, never prices.')+keyClientsHtml()+
  `<div class="search-row"><input id="clients-search" type="search" placeholder="Search client, town, postal code…" aria-label="Search clients" value="${esc(clientsQuery)}"><span class="badge gray">${all.length} clients with routes</span><div class="view-toggle">${sorts}</div></div>
  <section class="panel"><div class="schedule"><table class="clients-table"><thead><tr><th>Client</th><th>Routes</th><th>Orders</th><th>Requests</th><th>Form link</th><th></th></tr></thead><tbody>
  ${list.map(c=>{const p=c.place,phone=waPhone(p.phone);return `<tr><td><strong>${esc(p.company)}</strong><small>${esc(p.city)} · ${esc(p.postal_code)}${p.phone?' · '+esc(p.phone):''}</small></td>
    <td><strong>${c.routes}</strong><small>${c.from.size} loading ${c.from.size===1?'point':'points'}</small></td><td><strong>${c.orders||'—'}</strong>${c.last?`<small>last ${formatDay(dayOf(c.last))}</small>`:''}</td><td>${c.requests||'—'}</td>
    <td>${p.client_token?badge('✓ Link made'):badge('Not sent yet','gray')}</td>
    <td class="clients-actions"><button class="btn small primary" data-action="client-copy" data-id="${p.id}">⧉ Copy link</button>${phone?`<button class="btn small" data-action="client-wa" data-id="${p.id}">💬 WhatsApp</button>`:''}${p.phone?`<a class="btn small" href="${esc(telHref(p.phone))}">☎</a>`:''}<button class="btn small" data-action="client-link" data-id="${p.id}">⋯</button></td></tr>`;}).join('')||`<tr><td colspan="6">${empty('No client matches','Change the search.','','')}</td></tr>`}
  </tbody></table></div></section>`;}
// The client's link (made on first use) and its public address.
async function clientUrl(clientId){const result=await api('/api/client-link',{client_id:Number(clientId)});const p=getLocation(clientId);if(p)p.client_token=result.token;return result.url||`${location.origin}/c/${result.token}`;}
async function copyClientLink(clientId){const url=await clientUrl(clientId);try{await navigator.clipboard.writeText(url);toast(`Link of ${getLocation(clientId)?.company||'the client'} copied. Paste it in WhatsApp or an email.`);}catch{prompt('Copy the link:',url);}if(view==='clients')render();}
// WhatsApp to the client: the form link, or a "truck free near you" offer from Next loads.
async function sendClientWhatsapp(clientId,offer=null){const win=window.open('','_blank');const p=getLocation(clientId),url=await clientUrl(clientId),phone=waPhone(p?.phone);
  const text=offer?CLIENT_OFFER.es(offer.place,offer.when,url):`Hola, para pedir transporte podéis usar este enlace (solo vuestras rutas): ${url}`;
  const href=`https://wa.me/${phone||''}?text=${encodeURIComponent(text)}`;if(win)win.location=href;else location.href=href;if(view==='clients')render();}
// "Next loads": the clients with saved routes loading near the free truck, closest first, ready to call or message.
function clientsToCallHtml(d){const by=new Map();
  for(const r of d.routes){const id=r.tariff.client_id;if(!id)continue;const c=by.get(id)||{id,routes:0,km:Infinity,from:null,best:null,orders:0};c.routes++;c.orders+=r.orders||0;if(r.empty_km<c.km){c.km=r.empty_km;c.from=getLocation(r.tariff.from_id);}if(r.per_hour!=null&&(c.best==null||r.per_hour>c.best))c.best=r.per_hour;by.set(id,c);}
  const list=[...by.values()].map(c=>({...c,place:getLocation(c.id)})).filter(c=>c.place).sort((a,b)=>a.km-b.km||b.orders-a.orders).slice(0,8);if(!list.length)return '';
  return `<div class="call-clients"><h4>📞 Clients to call · ${by.size} with routes loading ${withinText(d.radius_km)}</h4>${list.map(c=>{const phone=waPhone(c.place.phone);return `<div class="call-client"><div><strong>${esc(c.place.company)}</strong><small>${c.routes} ${c.routes===1?'route':'routes'} · closest loading ${esc(c.from?.city||'?')} ≈ ${km(Math.round(c.km))} empty${c.best!=null?` · up to ${euro(c.best)}/h`:''}${c.orders?` · done ${c.orders}×`:''}</small></div>
    <div class="call-actions">${c.place.phone?`<a class="btn small" href="${esc(telHref(c.place.phone))}">☎ Call</a>`:''}${phone?`<button class="btn small primary" data-action="client-offer" data-id="${c.id}">💬 Offer the truck</button>`:''}<button class="btn small" data-action="client-copy" data-id="${c.id}">⧉ Link</button></div></div>`;}).join('')}</div>`;}
function nextLoadOffer(){const d=nextLoadsData,origin=getLocation(nearbyQuery?.location_id);const when=d?.from?new Date(d.from).toLocaleDateString('es-ES',{weekday:'long',day:'numeric',month:'numeric',timeZone:'Europe/Madrid'}):'';
  return {place:origin?.city||'',when:when?`el ${when}`:''};}
