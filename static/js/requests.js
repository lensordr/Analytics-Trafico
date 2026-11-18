// ---------------------------------------------------------------- Client requests
// Clients ask for transport from their own link (/c/<token>): only their routes, no prices. Here the dispatcher sees
// the request with the matching tariff and the closest tractors, and creates the order (linked to the request).
let requestFilter='New';
const REQUEST_STATUS={New:['New','amber'],Planned:['Planned',''],Rejected:['Rejected','gray']};
const REPLY_TEXT={
  es:{ok:(r,v)=>`Hola, confirmamos la carga del ${r.day}${v?` con el camión ${v}`:''}: ${r.route}. Gracias.`,no:(r,why)=>`Hola, lo sentimos, no podemos hacer la carga del ${r.day} (${r.route})${why?': '+why:''}.`},
  en:{ok:(r,v)=>`Hello, we confirm the loading on ${r.day}${v?` with truck ${v}`:''}: ${r.route}. Thank you.`,no:(r,why)=>`Hello, sorry, we cannot do the loading on ${r.day} (${r.route})${why?': '+why:''}.`}};
const loadRequests=()=>state.load_requests||[];
function requestCount(){return loadRequests().filter(r=>r.status==='New').length;}
function updateRequestCount(){const el=$('#nav-requests');if(!el||!state)return;const n=requestCount();el.hidden=!n;el.textContent=n;el.title=`${n} new client ${n===1?'request':'requests'}`;}
function requestPlace(id,typed){const p=getLocation(id);return p?{p,label:`${p.company} · ${p.city}`}:{p:null,label:typed||'?'};}
function requestRoute(r){return `${requestPlace(r.from_id,r.from_text).label} → ${requestPlace(r.to_id,r.to_text).label}`;}
function requestDayLabel(r){return new Date(r.load_date+'T12:00:00').toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'2-digit'})+(r.load_time?' · '+r.load_time:'');}
// "24 t", "26,5t", "24000 kg" → kg for the order form; anything else stays in the notes.
function requestKg(weight){const m=String(weight||'').replace(',','.').match(/^\s*(\d+(?:\.\d+)?)\s*(t|tn|ton|tons|toneladas|kg)?\s*$/i);if(!m)return null;const v=Number(m[1]),unit=(m[2]||'t').toLowerCase();const kg=unit==='kg'?v:v*1000;return kg>=1000&&kg<=60000?kg:null;}
function requestPickup(r){const time=/^\d{1,2}[:.]\d{2}/.test(r.load_time)?r.load_time.slice(0,5).replace('.',':').padStart(5,'0'):'08:00';return `${r.load_date}T${time}`;}
function requestTariff(r){return r.tariff_id?(state.tariffs||[]).find(t=>t.id===r.tariff_id):null;}
function requestTractors(r){const from=getLocation(r.from_id);if(!from||from.lat==null)return '';const list=suggestTractors(from.id,dispatchInputInstant(requestPickup(r)),0);if(!list||!list.length)return '';
  return `<div class="req-suggest"><span>Closest tractors</span>${list.slice(0,3).map(s=>`<div class="${s.late?'late':''}"><strong>${esc(s.t.plate)}</strong> ${esc(s.driver?.name||'')} · ≈ ${km(Math.round(s.km))} empty from ${esc(s.from.city)} · ${s.late?'⚠ arrives '+formatDate(dispatchStamp(s.arrive)):'✓ in time'}</div>`).join('')}</div>`;}
function requestReplyHref(r,kind,extra=''){const client=getLocation(r.client_id),phone=waPhone((r.contact.match(/\+?[\d][\d\s-]{7,}/)||[])[0]||client?.phone||'');if(!phone)return '';
  const lang=r.lang==='en'?'en':'es',info={day:new Date(r.load_date+'T12:00:00').toLocaleDateString(lang==='en'?'en-GB':'es-ES',{weekday:'long',day:'numeric',month:'long'}),route:requestRoute(r)};
  return `https://wa.me/${phone}?text=${encodeURIComponent(REPLY_TEXT[lang][kind](info,extra))}`;}
function requestCard(r){const client=getLocation(r.client_id),[label,tone]=REQUEST_STATUS[r.status]||[r.status,'gray'],tariff=requestTariff(r),newRoute=!r.from_id||!r.to_id;
  const orders=r.order_ids.map(id=>state.orders.find(o=>o.id===id)).filter(Boolean),plates=orders.map(o=>o.plate).join(', ');
  const ok=requestReplyHref(r,'ok',plates);
  const facts=[['Loading day',requestDayLabel(r)],['Unload by',r.unload_by?new Date(r.unload_by+'T12:00:00').toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'2-digit'}):''],['Trucks',r.trucks],['Weight',r.weight],['Material',r.material],['Client reference',r.reference],['Contact',r.contact]].filter(([,v])=>v!==''&&v!=null);
  return `<article class="req-card ${r.status.toLowerCase()}"><div class="req-head"><div><strong>${esc(client?.company||'?')}</strong><small>Sent ${formatDate(r.created_at)} · ${r.lang==='en'?'English':'Spanish'}</small></div><div>${badge(label,tone)}${newRoute?badge('New route · no tariff','amber'):''}</div></div>
  <div class="req-route"><div><span>Loading</span><strong>${esc(requestPlace(r.from_id,r.from_text).label)}</strong>${r.from_id?'':`<small>${esc(r.from_text)}</small>`}</div><div class="req-arrow">→</div><div><span>Unloading</span><strong>${esc(requestPlace(r.to_id,r.to_text).label)}</strong>${r.to_id?'':`<small>${esc(r.to_text)}</small>`}</div></div>
  <div class="req-facts">${facts.map(([k,v])=>`<div><span>${k}</span>${esc(String(v))}</div>`).join('')}${tariff?`<div><span>Tariff (only you see it)</span>${esc(rateText(tariff.basis,tariff.rate))}${tariff.km?` · ${km(tariff.km)}`:''}</div>`:''}</div>
  ${r.notes?`<p class="req-notes">${esc(r.notes)}</p>`:''}${r.status==='Rejected'&&r.reason?`<p class="req-notes muted">Reason sent to the client: ${esc(r.reason)}</p>`:''}
  ${orders.length?`<p class="req-orders">Orders: ${orders.map(o=>`<button class="link-button" data-action="order-detail" data-id="${o.id}">${esc(o.number)} · ${esc(o.plate)}</button>`).join(' ')} ${orders.length<r.trucks?`<span class="muted">(${orders.length} of ${r.trucks} trucks)</span>`:''}</p>`:''}
  ${r.status==='New'?requestTractors(r):''}
  <div class="actions">${r.status!=='Rejected'&&orders.length<r.trucks?`<button class="btn small primary" data-action="req-order" data-id="${r.id}">＋ Create order${r.trucks>1?` (${orders.length+1} of ${r.trucks})`:''}</button>`:''}
  ${r.status==='Planned'&&ok?`<a class="btn small" href="${esc(ok)}" target="_blank" rel="noopener">💬 Confirm to client</a>`:''}
  ${r.status==='New'?`<button class="btn small" data-action="req-status" data-id="${r.id}" data-status="Planned">✓ Mark planned</button><button class="btn small" data-action="req-reject" data-id="${r.id}">✕ Reject</button>`:`<button class="btn small" data-action="req-status" data-id="${r.id}" data-status="New">↺ Open again</button>`}
  ${client?`<button class="btn small" data-action="client-link" data-id="${client.id}">🔗 Client link</button>`:''}</div></article>`;}
function requestsView(){const all=loadRequests(),counts={New:0,Planned:0,Rejected:0};for(const r of all)counts[r.status]=(counts[r.status]||0)+1;
  const shown=all.filter(r=>requestFilter==='All'||r.status===requestFilter);
  const tabs=['New','Planned','Rejected','All'].map(f=>`<button class="${requestFilter===f?'selected':''}" data-action="req-filter" data-id="${f}">${f}${f==='All'?'':` · ${counts[f]||0}`}</button>`).join('');
  return `<section class="panel"><div class="panel-head"><div><h2>📨 Client requests</h2><p>Sent by clients from their own link. They see only their routes, never prices. Make a link from the client's card under Saved locations.</p></div><div class="view-toggle">${tabs}</div></div>
  <div class="req-list">${shown.map(requestCard).join('')||`<div class="empty"><div class="empty-symbol">📨</div><h3>No ${requestFilter==='All'?'':requestFilter.toLowerCase()+' '}requests</h3><p>Send a client their link: Saved locations → the client → 🔗 Client link.</p></div>`}</div></section>`;}
function openRequestOrder(id){const r=loadRequests().find(x=>x.id===Number(id));if(!r)return;const kg=requestKg(r.weight),tariff=requestTariff(r);
  const notes=[r.reference&&`Client ref: ${r.reference}`,r.material&&`Material: ${r.material}`,!kg&&r.weight&&`Weight: ${r.weight}`,r.unload_by&&`Unload by ${r.unload_by}`,!r.from_id&&`Loading (to add): ${r.from_text}`,!r.to_id&&`Unloading (to add): ${r.to_text}`,r.contact&&`Contact: ${r.contact}`,r.notes].filter(Boolean).join('\n');
  openOrder({loading_id:r.from_id||'',unloading_id:r.to_id||'',pickup_at:requestPickup(r),client_id:r.client_id,loading_weight_kg:kg||undefined,notes});
  const form=$('#order-form');if(!form)return;form.insertAdjacentHTML('afterbegin',`<input type="hidden" name="request_id" value="${r.id}">`);
  if(tariff&&typeof applyTariff==='function')applyTariff(form,tariff);
  for(const name of ['loading_id','unloading_id'])form.elements[name]?.dispatchEvent(new Event('change',{bubbles:true}));
  if(tariff&&typeof applyTariff==='function')applyTariff(form,tariff);
  toast(`From ${getLocation(r.client_id)?.company||'the client'}'s request: choose the tractor (✨ Suggest the closest tractor) and save.`);}
async function openClientLink(clientId,renew=false){const client=getLocation(clientId);const result=await api('/api/client-link',{client_id:Number(clientId),renew});await refresh();
  const url=result.url||`${location.origin}/c/${result.token}`;
  const text={es:`Hola, para pedir transporte puedes usar este enlace (solo tus rutas): ${url}`,en:`Hello, to request transport you can use this link (your routes only): ${url}`};
  const phone=waPhone(client?.phone);
  showModal('Client order form · '+(client?.company||''),`<p class="subtitle">${result.routes} saved ${result.routes===1?'route':'routes'} for this client. The client sees the company and town of each loading and unloading, never prices or other clients' routes. A different place can be typed: it arrives as a new route.</p>
  ${result.routes?'':notice(`<strong>${esc(client?.company||'')}</strong> has no saved routes as client (Tariffs → Client). The form still works, but the client must type every address.${result.related?.length?`<br>The routes are saved under ${result.related.map(r=>`<button class="link-button" data-action="client-link" data-id="${r.id}">${esc(r.company)} (${r.routes} routes)</button>`).join(', ')}: send that link instead.`:''}`,true)}
  <div class="field"><label>Link (secret: send it only to this client)</label><input readonly value="${esc(url)}" id="client-link-url"></div>
  <form class="actions client-link-actions"><textarea name="text" hidden>${esc(text.es)}</textarea><button type="button" class="btn small primary" data-action="copy-client-link">⧉ Copy link</button>
  ${['es','en'].map(l=>`<a class="btn small" target="_blank" rel="noopener" href="https://wa.me/${phone||''}?text=${encodeURIComponent(text[l])}">💬 WhatsApp ${l.toUpperCase()}</a>`).join('')}
  <a class="btn small" href="${esc(url)}" target="_blank" rel="noopener">↗ Open as the client</a></form>
  <p class="subtitle">Link sent to the wrong person? Make a new one: the old link stops working at once.</p>
  <div class="actions"><button class="btn small" data-action="client-link-renew" data-id="${client?.id}">↻ New link</button><button class="btn small danger" data-action="client-link-revoke" data-id="${client?.id}">Stop the link</button></div>`,'CLIENT REQUESTS / LINK');}
