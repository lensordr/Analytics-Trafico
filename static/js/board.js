'use strict';
// Order stops and delivery alerts.
const stopIcon={Loading:'↗',Unloading:'↙','Trailer swap':'⇄','Tractor change':'⛟'};
function orderStops(o){if(o.stops?.length)return o.stops;const done=o.status==='Completed'?'Done':'Planned';return [{seq:1,kind:'Loading',location_id:o.loading_id,planned_at:o.pickup_at,status:done,done_at:null,leg_km:0},{seq:2,kind:'Unloading',location_id:o.unloading_id,planned_at:o.unloading_at,status:done,done_at:null,leg_km:o.distance_km}];}
const deliveryDay=o=>dayOf(o.delivered_at||o.unloading_at);
// Tractor parts of an order, as on the server: a tractor change ends one tractor's part (when it leaves the trailer)
// and starts the next one's. [{truck_id,driver_id,first,last,start,end}]; one part when there is no change.
function orderParts(o){const stops=orderStops(o),parts=[];let cur={truck_id:o.truck_id,driver_id:o.driver_id,first:1,start:o.pickup_at};
  for(const s of stops)if(s.kind==='Tractor change'){parts.push({...cur,last:s.seq,end:s.done_at||s.planned_end_at});cur={truck_id:s.truck_id||null,driver_id:s.driver_id||null,first:s.seq,start:s.done_at||s.planned_at};}
  parts.push({...cur,last:stops[stops.length-1]?.seq||1,end:o.available_at});return parts;}
const hasRelay=o=>orderStops(o).some(s=>s.kind==='Tractor change');
const partStops=(o,p)=>orderStops(o).filter(s=>s.seq>=p.first&&s.seq<=p.last);
// Tractor, driver and carrier of a part (the order's own for the first part).
function partVehicle(o,p){const t=state.trucks.find(x=>x.id===p.truck_id),d=state.drivers.find(x=>x.id===p.driver_id),own=p.first===1;
  return {plate:p.truck_id?(t?.plate||o.plate):'No tractor yet',driver_name:d?.name||(own?o.driver_name:'')||'Driver not set',carrier:d?.carrier||(own?o.carrier:'')};}
// The part a stop belongs to: a tractor change belongs to the tractor that brings the trailer there.
const stopPart=(o,s)=>orderParts(o).find(p=>s.seq>=p.first&&s.seq<=p.last)||orderParts(o)[0];
const nextPart=(o,s)=>orderParts(o).find(p=>p.first===s.seq&&s.kind==='Tractor change');
function relayText(o,s){const p=nextPart(o,s);if(!p)return '';const v=partVehicle(o,p);return p.truck_id?`${v.plate} · ${v.driver_name} takes the trailer`:'No tractor chosen yet · the loaded trailer waits here';}
function nextStop(o){return ['Cancelled','Completed'].includes(o.status)?null:orderStops(o).find(s=>s.status!=='Done')||null;}
function lastDoneStop(o){return orderStops(o).filter(s=>s.status==='Done').pop()||null;}
// First stop of an order that only picks up a loaded, parked trailer.
const trailerPickupStop=(o,s)=>!!o.trailer_pickup&&s.seq===1&&s.kind==='Loading';
function stopKindLabel(o,s){const n=orderStops(o).length;return trailerPickupStop(o,s)?'Pick up loaded trailer':s.kind==='Tractor change'?'Tractor change':s.kind==='Trailer swap'?'Trailer swap':s.kind==='Loading'?(s.seq===1?'Loading':'Extra loading'):(s.seq===n?'Unloading':'Extra unloading');}
function confirmLabel(o,s){return trailerPickupStop(o,s)?'✓ Trailer picked up':s.kind==='Tractor change'?'✓ Trailer handed over':s.kind==='Loading'?'✓ Loaded':s.kind==='Trailer swap'?'✓ Swap done':s.seq===orderStops(o).length?'✓ Delivered':'✓ Unloaded';}
function trailerChain(o){return [o.trailer||'no trailer',...orderStops(o).filter(s=>s.kind==='Trailer swap').map(s=>s.pick_trailer)].join(' → ');}
function routeText(o){return orderStops(o).map(s=>stopIcon[s.kind]+' '+(getLocation(s.location_id)?.city||'?')).join(' → ');}
// Delivery alerts: blue when an in-transit order is within 2 h of its planned unloading,
// red when the planned unloading time has passed and the delivery is not confirmed.
const ARRIVING_MS=2*3600000;
function duration(ms){const m=Math.max(0,Math.round(ms/60000)),d=Math.floor(m/1440),h=Math.floor(m%1440/60),r=m%60;return d?`${d}d ${h}h`:h?`${h}h${r?' '+String(r).padStart(2,'0')+'m':''}`:`${r}m`;}
function deliveryAlert(o,now=Date.now()){if(!o||o.status==='Completed'||o.status==='Cancelled')return null;const stops=orderStops(o),last=stops[stops.length-1];if(!last||last.status==='Done')return null;const due=Date.parse(last.planned_at);if(now>due)return {kind:'overdue',ms:now-due,stop:last,label:`Overdue · +${duration(now-due)}`,title:`Planned unloading ${formatDate(last.planned_at)} has passed and the delivery is not confirmed.`};if(o.status==='In transit'&&due-now<=ARRIVING_MS)return {kind:'arriving',ms:due-now,stop:last,label:`In transit · ${duration(due-now)} to unloading`,title:`Planned unloading ${formatDate(last.planned_at)}.`};return null;}
function orderStatusBadge(o){const alert=deliveryAlert(o);if(alert)return `<span class="badge status-alert ${alert.kind}" title="${esc(alert.title)}">${alert.kind==='overdue'?'⚠':'⏱'} ${esc(alert.label)}</span>`;return badge(o.status,{Cancelled:'gray',Completed:'st-done','In transit':'st-transit',Planned:'st-planned'}[o.status]||'');}
function deliveryAlerts(){return state.orders.map(o=>({o,alert:deliveryAlert(o)})).filter(x=>x.alert).sort((a,b)=>(a.alert.kind==='overdue'?0:1)-(b.alert.kind==='overdue'?0:1)||(a.alert.kind==='overdue'?b.alert.ms-a.alert.ms:a.alert.ms-b.alert.ms));}
function alertsPanel(){const list=deliveryAlerts();if(!list.length)return '';const overdue=list.filter(x=>x.alert.kind==='overdue').length;
  return `<section class="panel alerts-panel ${overdue?'has-overdue':''}"><div class="panel-head"><div><h2>${overdue?'⚠ ':'⏱ '}Deliveries to follow up <span class="section-count">${list.length}</span></h2><p>${overdue?`${overdue} overdue: planned unloading passed, delivery not confirmed. `:''}${list.length-overdue?`${list.length-overdue} arriving within 2 hours. `:''}Call the driver and the client, then confirm the delivery or change the ETA.</p></div></div><div class="alert-list">${list.map(({o,alert})=>{const driver=state.drivers.find(d=>d.id===o.driver_id),place=getLocation(alert.stop.location_id);const tel=(label,phone)=>phone?`<a class="btn small" href="${esc(telHref(phone))}">☎ ${label}</a>`:'';
    return `<div class="alert-row ${alert.kind}"><span class="badge status-alert ${alert.kind}">${alert.kind==='overdue'?'⚠':'⏱'} ${esc(alert.label)}</span><div class="alert-what"><button class="link-button" data-action="order-detail" data-id="${o.id}">${esc(o.number)}</button> · <strong>${esc(o.plate)}</strong> · ${esc(o.driver_name)}<small>Unloading ${esc(place?.company||'')} · ${esc(place?.city||'')} · planned ${formatDate(alert.stop.planned_at)}</small></div><div class="alert-actions">${tel('Driver',driver?.phone)}${tel('Client',place?.phone)}<button class="btn small" data-action="order-detail" data-id="${o.id}">Open · confirm / ETA</button></div></div>`;}).join('')}</div></section>`;}
function updateAlertCount(){const list=deliveryAlerts(),overdue=list.filter(x=>x.alert.kind==='overdue').length,el=$('#nav-alerts');if(!el)return;/* replaced by the To do count */el.hidden=!list.length;el.textContent=list.length;el.className=overdue?'overdue':'arriving';el.title=`${overdue} overdue · ${list.length-overdue} arriving within 2 h`;}
function stopIsLate(s){return s.status!=='Done'&&Date.parse(s.planned_at)<Date.now();}
function trailerWhere(t){if(t.archived_at)return 'Archived';if(t.parked_location_id){const p=getLocation(t.parked_location_id);return `Parked · ${p?.city||'?'}${t.parked_at?' since '+formatDate(t.parked_at):''}`;}const on=state.trucks.filter(x=>x.trailer_id===t.id&&!x.archived_at).map(x=>x.plate);return on.length?'On tractor '+on.join(', '):'Position not set';}
function filteredOrders(){const has=(o,kind,text)=>!text||orderStops(o).some(s=>s.kind===kind&&placeLabel(getLocation(s.location_id)).toLowerCase().includes(text.toLowerCase()));return state.orders.filter(order=>
  orderParts(order).some(p=>partVehicle(order,p).plate.toLowerCase().includes(filters.plate.toLowerCase()))&&has(order,'Loading',filters.loading)&&has(order,'Unloading',filters.unloading)&&(!filters.operation||order.operation===filters.operation)&&(filters.status?order.status===filters.status:order.status!=='Cancelled'));}
function opportunities(){const endDay=addDays(selectedDay,period-1),list=[];const orders=state.orders.filter(o=>o.status!=='Cancelled');
  for(const order of orders){const truck=state.trucks.find(t=>t.id===order.truck_id);if(!truck?.active)continue;
    const ready=order.available_at;
    const next=orders.filter(o=>o.id!==order.id&&(o.truck_id===order.truck_id||o.driver_id===order.driver_id||(order.trailer&&o.trailer.toLowerCase()===order.trailer.toLowerCase()))&&new Date(o.pickup_at)>=new Date(ready)).sort((a,b)=>new Date(a.pickup_at)-new Date(b.pickup_at))[0];
    const from = new Date(ready)>new Date(state.now)?ready:state.now;
    if(next)continue; // the next loading is already planned: nothing left to recommend for this unloading
    if(dayOf(from)>endDay|| (next&&dayOf(next.pickup_at)<selectedDay))continue;
    if(filters.plate&&!order.plate.toLowerCase().includes(filters.plate.toLowerCase()))continue;
    if(filters.operation&&order.operation!==filters.operation)continue;
    if(filters.loading||filters.unloading){if(!placeLabel(getLocation(order.unloading_id)).toLowerCase().includes((filters.unloading||filters.loading).toLowerCase()))continue;}
    list.push({order,from,next});
  }
  return list.sort((a,b)=>new Date(a.from)-new Date(b.from));
}
function orderTouchesDay(o,day){return orderStops(o).some(s=>dayOf(s.planned_at)===day||(s.done_at&&dayOf(s.done_at)===day))||(dayOf(o.pickup_at)<=day&&deliveryDay(o)>=day);}
function dayStops(orders,kind){const list=[];for(const order of orders)for(const stop of orderStops(order))if((stop.kind===kind||(kind==='Trailer swap'&&stop.kind==='Tractor change'))&&dayOf(stop.planned_at)===selectedDay)list.push({order,stop});return list.sort((a,b)=>Date.parse(a.stop.planned_at)-Date.parse(b.stop.planned_at));}
function dailyStopSection(entries,kind){const loading=kind==='Loading',swap=kind==='Trailer swap',key=swap?'swap':loading?'loading':'unloading',dateIsToday=selectedDay===dayOf(state.now),title=swap?'Trailer swaps & tractor changes':kind,label=dateIsToday?title+' today':title+' · '+formatDay(selectedDay);if(swap&&!entries.length)return '';
  const help=swap?'Trailer swap: a truck leaves one trailer and picks up another. Tractor change: the same loaded trailer goes on with another tractor; the first tractor is free from here.':loading?'Every loading stop on this date (first and extra loadings), ordered by time. Confirm with ✓ Loaded.':'Every unloading stop on this date, ordered by estimated time. ✓ Delivered on the last stop completes the order.';
  return `<section class="panel daily-stop-section daily-${key}" data-daily-section="${key}"><div class="panel-head"><div><h2><span class="daily-stop-icon">${stopIcon[kind]}</span> ${label} <span class="section-count">${entries.length} ${entries.length===1?'stop':'stops'}</span></h2><p>${help}</p></div>${badge(swap?'Swap':kind,swap?'blue':loading?'':'amber')}</div>${entries.length?`<div class="schedule journey-schedule"><table><thead><tr><th>Truck & driver · this stop</th><th>Complete journey · all stops</th><th>Available after delivery</th><th>Order · confirm stop</th></tr></thead><tbody>${entries.map(entry=>journeyRow({...entry,day:selectedDay})).join('')}</tbody></table></div>`:empty(loading?'No loading on this day':'No unloading on this day','No stops match this date and the selected filters.')}</section>`;}
