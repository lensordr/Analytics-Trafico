'use strict';
// ---------------------------------------------------------------- Key clients (Witruck history)
// The Witruck exports (Rutas → Exportar and Rutas → Facturación) imported here: which clients give the most trips,
// how it moves week by week, how much goes to partners, and in "Next loads" the lanes they load near a free truck.
let keyClientsData=null,keyClientsLoading=false;
async function loadKeyClients(force=false){if(keyClientsLoading||(keyClientsData&&!force))return;keyClientsLoading=true;
  try{keyClientsData=await api('/api/key-clients',{});}catch(error){keyClientsData={clients:[],total:0,error:error.message};}finally{keyClientsLoading=false;}
  if(view==='clients')render();}
const weekLabel=iso=>new Date(iso+'T12:00:00').toLocaleDateString('en-GB',{day:'numeric',month:'short'});
function trendBadge(trend){if(trend==null)return '';if(trend>=15)return badge(`▲ ${trend}%`);if(trend<=-15)return badge(`▼ ${Math.abs(trend)}%`,'amber');return badge('≈ steady','gray');}
function keyClientsHtml(){const d=keyClientsData;if(!d){loadKeyClients();return `<section class="panel padded key-clients"><p class="subtitle">Loading the key clients…</p></section>`;}
  const head=`<div class="panel-head"><div><h2>⭐ Key clients · last ${d.weeks?.length||4} weeks (Witruck)</h2><p>${d.total?`${d.total} trips, ${d.own} with our trucks (${Math.round(d.own/d.total*100)}%), the rest with partners. Last trip ${formatDay(d.last_day)} · imported ${formatDate(d.imported_at)}.`:'Import the Witruck exports to see which clients give the most trips.'}</p></div><button class="btn small primary" data-action="witruck-import">⇪ Import Witruck</button></div>`;
  if(d.error)return `<section class="panel key-clients">${head}${notice(esc(d.error),true)}</section>`;
  if(!d.clients.length)return `<section class="panel key-clients">${head}</section>`;
  const weeks=d.weeks.map((w,i)=>`<th class="num">${i===d.weeks.length-1?'This wk':weekLabel(w)}</th>`).join('');
  const rows=d.clients.slice(0,15).map(c=>{const place=c.client_id?getLocation(c.client_id):null,phone=waPhone(place?.phone);
    return `<tr><td><strong>${esc(c.name)}</strong><small>${c.lanes.map(l=>`${esc(l.lane)} ×${l.trips}`).join(' · ')}${c.material?' · '+esc(c.material):''}</small></td>
    ${c.weeks.map(n=>`<td class="num">${n||'—'}</td>`).join('')}<td>${trendBadge(c.trend)}</td>
    <td class="num"><strong>${c.trips}</strong><small>${c.own} own · ${c.trips-c.own} partners</small></td><td class="num">${c.revenue?euro(c.revenue):'—'}${c.priced&&c.priced<c.trips?`<small>${c.priced} of ${c.trips} priced</small>`:''}</td>
    <td class="clients-actions">${place?`${place.phone?`<a class="btn small" href="${esc(telHref(place.phone))}">☎</a>`:''}${phone?`<button class="btn small" data-action="client-wa" data-id="${place.id}">💬</button>`:''}${c.has_routes?`<button class="btn small" data-action="client-copy" data-id="${place.id}">⧉ Link</button>`:''}`:badge('Not in saved places','amber')}</td></tr>`;}).join('');
  return `<section class="panel key-clients">${head}<div class="schedule"><table class="clients-table key-table"><thead><tr><th>Client · main lanes</th>${weeks}<th>Trend</th><th class="num">Trips</th><th class="num">Revenue</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
  <p class="subtitle key-note">Trend: last full week against the week before. Revenue: invoiced, or the price × 24 t when not invoiced yet. Only you see it.</p></section>`;}
function witruckImportModal(){showModal('Import Witruck',`<p class="subtitle">In Witruck open <strong>Rutas</strong>, filter the last 4–6 weeks and press both <strong>⤓ Exportar</strong> (loading and unloading companies) and <strong>⤓ Facturación</strong> (client and price). Choose both files here (data.csv and data (1).csv). Importing again updates the same routes, never duplicates them.</p>
  <div class="field"><label for="witruck-files">Witruck files (.csv)</label><input type="file" id="witruck-files" accept=".csv,text/csv" multiple></div><p class="subtitle" id="witruck-status"></p>
  <div class="form-actions"><button class="btn" data-action="close">Close</button></div>`,'CLIENTS / WITRUCK');}
async function importWitruckFiles(files){const status=$('#witruck-status');try{if(!files.length)return;if([...files].some(f=>f.size>1500000))throw new Error('A file is too large (max 1.5 MB). Filter fewer weeks in Witruck.');
    if(status)status.textContent='Importing…';const texts=await Promise.all([...files].map(f=>f.text()));const result=await api('/api/witruck-import',{files:texts});
    $('#modal').close();toast(result.message);await loadKeyClients(true);}catch(error){if(status)status.textContent='';toast(error.message,true);}}
// "Next loads": the key clients' lanes loading near the free truck (from the Witruck history).
function keyLanesHtml(d){const lanes=d.market||[];if(!lanes.length)return '';
  const place=p=>`<strong>${esc(p.city||'?')}</strong> <small class="inline">${esc(p.company)}</small>`;
  return `<div class="call-clients key-lanes"><h4>⭐ Key clients loading near here · Witruck, last 4 weeks</h4>${lanes.map((l,i)=>{const client=l.client_id?getLocation(l.client_id):null,phone=waPhone(client?.phone),partners=l.trips-l.own;
    return `<div class="call-client"><div><strong>${esc(l.client)}</strong> <span class="lane">${place(l.from)} → ${place(l.to)}</span>
    <small>${l.trips}× in 4 weeks · last ${formatDay(l.last)} · ${km(Math.round(l.empty_km))} empty${l.per_trip!=null?` · ≈ ${euro(l.per_trip)}/trip`:''}${l.material?' · '+esc(l.material):''}</small>
    <small>${l.own?`${l.own} with our trucks`:'never with our trucks'}${partners?` · <span class="partner">${partners} with partners${l.carriers.length?' ('+esc(l.carriers.join(', '))+')':''}: we could take them</span>`:''}${l.onward!=null?` · then ≈ ${Math.round(l.onward/4)} loads/week near ${esc(l.to.city)}`:''}</small></div>
    <div class="call-actions">${client?.phone?`<a class="btn small" href="${esc(telHref(client.phone))}">☎ Call</a>`:''}${phone?`<button class="btn small primary" data-action="client-offer" data-id="${client.id}">💬 Offer the truck</button>`:''}${l.from.id&&l.to.id?`<button class="btn small" data-action="lane-order" data-index="${i}">＋ Order</button>`:''}</div></div>`;}).join('')}</div>`;}
function openLaneOrder(index){const l=nextLoadsData?.market?.[Number(index)];if(!l)return;const q=nearbyQuery||{};$('#modal').close();
  openOrder({loading_id:l.from.id,unloading_id:l.to.id,client_id:l.client_id||'',truck_id:q.truck_id||'',driver_id:q.driver_id||'',pickup_at:q.from||undefined});}
