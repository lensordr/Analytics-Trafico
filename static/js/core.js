'use strict';
// Shared state, formatting helpers, form fields, API calls, refresh and page routing.
const $ = (s, root=document) => root.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state, view='planner', selectedDay='', period=1, toastTimer, filterTimer;
let upcomingData=null, upcomingError='', upcomingRequest=0, weekOffset=0, weekCount=1, upcomingTruck='', upcomingOperation='';
const filters={plate:'',loading:'',unloading:'',operation:'',status:''};
const names={board:'Fleet board',planner:'Daily planning',upcoming:'Upcoming planning',analytics:'Analytics',trailers:'Trailers',trucks:'Tractors',drivers:'Drivers & carriers',locations:'Saved locations',materials:'Materials',itv:'ITV',settings:'Settings & backup'};
// Default time at a loading or unloading point (hours); trailer swaps default to 0.5 h.
const DEFAULT_HANDLING_HOURS=2;
const localParts = value => value ? String(value).slice(0,16) : '';
const dayOf = value => value ? String(value).slice(0,10) : '';
const addDays=(day,n)=>new Date(Date.parse(day+'T12:00:00Z')+n*86400000).toISOString().slice(0,10);
const formatDay=(day,full=false)=>new Intl.DateTimeFormat('en-GB',{weekday:full?'long':'short',day:'numeric',month:full?'long':'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'));
const formatTime=value=>value?new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:'Europe/Madrid'}).format(new Date(value)):'—';
const formatDate=value=>value?formatDay(dayOf(value))+' · '+formatTime(value):'—';
const hours=value=>Number(value).toLocaleString('en-GB',{maximumFractionDigits:1})+' h';
const km=value=>value==null?'Unknown':Number(value).toLocaleString('en-GB',{maximumFractionDigits:1})+' km';
const getLocation=id=>state.locations.find(x=>x.id===Number(id));
const placeLabel=p=>p?`${p.city} · ${p.company} · ${p.postal_code}, ${p.country}`:'Unknown location';
const locationHtml=id=>{const p=getLocation(id);return p?`<div class="place"><strong>${esc(p.city)} <span class="muted">${esc(p.postal_code)}</span></strong><small>${esc(p.company)} · ${esc(p.country)}</small></div>`:'—';};
const badge=(value,kind='')=>`<span class="badge ${kind}">${esc(value)}</span>`;
const errorBox='<div class="form-error" role="alert"></div>';
const options=(items,value,label,empty='Choose…')=>`<option value="">${esc(empty)}</option>`+items.map(item=>`<option value="${item.id}" ${Number(value)===item.id?'selected':''}>${esc(label(item))}</option>`).join('');
const field=(label,name,value='',type='text',attrs='',hint='')=>`<div class="field"><label for="f-${name}">${esc(label)}</label><input id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${attrs}>${hint?`<small>${hint}</small>`:''}</div>`;
const selectField=(label,name,body)=>`<div class="field"><label for="f-${name}">${label}</label><select id="f-${name}" name="${name}">${body}</select></div>`;
const notice=(message,warning=false)=>`<div class="notice ${warning?'warning':''}">${message}</div>`;
const locationKinds=[['Client','Client · gives loads'],['Warehouse','Warehouse / depot · gives loads'],['Other','Other · may give loads'],['Unloading only','Unloading only'],['Parking','Parking / truck stop']];
// Default loading time for a truck's next order: the chosen time, or later if the truck is still busy then.
const truckFreeFrom=(truckId,wanted)=>{const latest=state.orders.filter(o=>o.truck_id===truckId&&o.status!=='Cancelled').map(o=>o.available_at).sort((a,b)=>Date.parse(b)-Date.parse(a))[0];return latest&&Date.parse(latest)>new Date(wanted).getTime()?localParts(latest):wanted;};
const contactResults=['Had a load','No load now','No answer','Call back later'];
const kindLabel=kind=>(locationKinds.find(([value])=>value===(kind||'Client'))||[,kind])[1].split(' · ')[0];
const telHref=phone=>'tel:'+String(phone).replace(/[^\d+]/g,'');
const contactLines=p=>{const parts=[p.contact_name?esc(p.contact_name):'',p.phone?`<a href="${esc(telHref(p.phone))}">☎ ${esc(p.phone)}</a>`:'',p.email?`<a href="mailto:${esc(p.email)}">✉ ${esc(p.email)}</a>`:''].filter(Boolean);return (parts.length?`<p class="contact-line">${parts.join(' · ')}</p>`:'')+(p.notes?`<p class="contact-notes">${esc(p.notes)}</p>`:'');};
const lastContactLine=c=>c?`<p class="last-contact ${c.result==='Had a load'?'good':''}">Last contact ${formatDate(c.at)} · <strong>${esc(c.result)}</strong>${c.note?' · '+esc(c.note):''}</p>`:'';
const empty=(title,message,action='',label='')=>`<div class="empty"><div class="empty-symbol">▦</div><h3>${title}</h3><div>${message}</div>${action?`<button class="btn primary" data-action="${action}">${label}</button>`:''}</div>`;

function toast(message,error=false){const el=$('#toast');el.textContent=message;el.className=error?'error':'';el.style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.style.display='none',6500);}
async function api(path,data){const response=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':state.token}:{},body:data?JSON.stringify(data):undefined});if(response.status===401){location.href='/login';throw new Error('Please sign in again.');}const result=await response.json();if(!response.ok){const error=new Error(result.error||'Request failed');error.status=response.status;throw error;}return result;}
async function refresh(){state=await api('/api/state');const logout=$('#logout');if(logout)logout.hidden=!state.auth;if(!selectedDay)selectedDay=dayOf(state.now);$('#today-label').textContent=formatDay(dayOf(state.now),true);$('#nav-count').textContent=state.orders.filter(x=>x.status==='Planned'||x.status==='In transit').length;if(state.storage==='turso'){$('#storage-title').textContent='Cloud workspace';$('#storage-detail').textContent='Turso · Synced to the cloud';}render();if(view==='upcoming')await loadUpcoming();}
function navigate(next){view=next;render();$('#main').focus();if(next==='upcoming')loadUpcoming();}
function heading(title,subtitle,action='',label=''){return `<div class="heading"><div><div class="eyebrow">MALCOMTRANS / OPERATIONS</div><h1>${title}</h1><p class="subtitle">${subtitle}</p></div>${action?`<div class="actions"><button class="btn primary" data-action="${action}">＋ ${label}</button></div>`:''}</div>`;}
function render(){updateAlertCount();updateItvCount();for(const el of document.querySelectorAll('.nav'))el.classList.toggle('active',el.dataset.view===view);$('#breadcrumb').textContent=names[view];$('#main').innerHTML=view==='board'?boardView():view==='planner'?plannerView():view==='upcoming'?upcomingView():view==='settings'?settingsView():view==='analytics'?analyticsView():view==='itv'?itvView():view==='materials'?materialsView():directoryView();}
