'use strict';

/* Searchable dropdown ("combobox") that stores an id in a hidden input.
   settings:
     required, empty        — as before
     snapshotName           — hidden input that also receives the chosen plate/text
     placeholder            — input placeholder
     hiddenId, hiddenAttrs  — id / extra attributes for the hidden value input (filters)
     newLabel               — enables "+ Add new <newLabel> “typed text”"
     newField               — name of a hidden input that receives the typed text (created on save);
                              without it, choosing "Add new" fires a `search-select-new` event. */
function searchSelectField(label,name,entries,value,settings={}) {
  const selected=entries.find(entry=>String(entry.id)===String(value));
  const empty=settings.empty||'Unassigned';
  const option=(entry,index)=>`<button type="button" role="option" id="search-${name}-${index}" class="search-select-option" data-search-id="${esc(entry.id)}" data-search-label="${esc(entry.label)}" data-search-value="${esc(entry.value??entry.label)}" data-search-terms="${esc((entry.label+' '+(entry.hint||'')).toLowerCase())}" aria-selected="${String(entry.id)===String(value)}"><strong>${esc(entry.label)}</strong>${entry.hint?`<small>${esc(entry.hint)}</small>`:''}</button>`;
  const newOption=settings.newLabel?`<button type="button" role="option" id="search-${name}-new" class="search-select-option search-select-new" data-search-new data-search-terms="" hidden></button>`:'';
  const noResults=settings.newLabel?`No match. Keep typing to add a new ${esc(settings.newLabel)}.`:'No matching registration. Add it to the directory first.';
  return `<div class="field"><label for="f-${name}">${esc(label)}</label><div class="search-select" data-search-select="${name}" ${settings.newLabel?`data-new-label="${esc(settings.newLabel)}"`:''}><input id="f-${name}" class="search-select-input" type="text" role="combobox" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="false" aria-controls="search-options-${name}" autocomplete="off" placeholder="${esc(settings.placeholder||'Search registrations…')}" value="${esc(selected?.label||'')}" ${settings.required?'required':''}><input type="hidden" data-search-hidden name="${name}"${settings.hiddenId?` id="${settings.hiddenId}"`:''}${settings.hiddenAttrs?' '+settings.hiddenAttrs:''} value="${esc(selected?.id||'')}">${settings.snapshotName?`<input type="hidden" name="${settings.snapshotName}" data-search-snapshot value="${esc(selected?.value??selected?.label??'')}">`:''}${settings.newField?`<input type="hidden" name="${settings.newField}" data-search-newvalue value="">`:''}<button type="button" class="search-select-toggle" aria-label="Show ${esc(label.toLowerCase())} options">⌄</button><div class="search-select-menu" role="listbox" id="search-options-${name}" hidden>${option({id:'',label:empty,value:''},0)}${entries.map((entry,index)=>option(entry,index+1)).join('')}${newOption}<p class="search-no-results" hidden>${noResults}</p></div></div></div>`;
}

function trailerPicker(label,id,plate='') {
  const selected=id??(state.trailers||[]).find(trailer=>trailer.plate.toLowerCase()===String(plate).toLowerCase())?.id;
  const entries=(state.trailers||[]).filter(t=>(t.active&&!t.archived_at)||t.id===Number(selected)).map(trailer=>(
    {id:trailer.id,label:trailer.plate,value:trailer.plate,hint:[trailer.active?'':'Inactive',trailerWhere(trailer),trailer.notes].filter(Boolean).join(' · ')}));
  return searchSelectField(label,'trailer_id',entries,selected,{snapshotName:'trailer',empty:'No trailer assigned',newLabel:'trailer',newField:'new_trailer',placeholder:'Search or type a new registration…'});
}

const searchHidden=wrapper=>wrapper.querySelector('[data-search-hidden]');
function searchSelectOpen(wrapper,open=true) {
  wrapper.querySelector('.search-select-menu').hidden=!open;
  wrapper.querySelector('.search-select-input').setAttribute('aria-expanded',String(open));
  if(!open)wrapper.querySelector('.search-select-input').removeAttribute('aria-activedescendant');
}
function searchSelectFilter(wrapper,query) {
  const typed=query.trim();
  query=typed.toLowerCase();
  let visible=0,exact=false;
  for(const option of wrapper.querySelectorAll('.search-select-option:not([data-search-new])')) {
    option.hidden=!!query&&!option.dataset.searchTerms.includes(query);
    option.classList.remove('search-highlight');
    if(!option.hidden)visible++;
    if(option.dataset.searchId&&option.dataset.searchLabel.toLowerCase()===query)exact=true;
  }
  const add=wrapper.querySelector('[data-search-new]');
  if(add){
    add.hidden=!typed||exact;
    add.classList.remove('search-highlight');
    add.innerHTML=`<strong>＋ Add new ${esc(wrapper.dataset.newLabel)} “${esc(typed.toUpperCase())}”</strong><small>${wrapper.querySelector('[data-search-newvalue]')?'Created when you save':'Opens the form to add it'}</small>`;
    if(!add.hidden)visible++;
  }
  wrapper.querySelector('.search-no-results').hidden=visible>0;
}
function searchSelectChoose(wrapper,option,dispatch=true) {
  const input=wrapper.querySelector('.search-select-input');
  const hidden=searchHidden(wrapper);
  const snapshot=wrapper.querySelector('[data-search-snapshot]');
  const newValue=wrapper.querySelector('[data-search-newvalue]');
  if(option&&option.dataset.searchNew!==undefined){
    const typed=input.value.trim().toUpperCase();
    searchSelectOpen(wrapper,false);
    if(!newValue){wrapper.dispatchEvent(new CustomEvent('search-select-new',{bubbles:true,detail:{name:hidden.name,value:typed}}));return;}
    hidden.value='';newValue.value=typed;input.value=typed;input.setCustomValidity('');
    if(snapshot)snapshot.value=typed;
    for(const row of wrapper.querySelectorAll('.search-select-option'))row.setAttribute('aria-selected',String(row===option));
    if(dispatch)hidden.dispatchEvent(new Event('change',{bubbles:true}));
    return;
  }
  if(newValue)newValue.value='';
  hidden.value=option?.dataset.searchId||'';
  input.value=hidden.value?option.dataset.searchLabel:'';
  input.setCustomValidity('');
  if(snapshot)snapshot.value=option?.dataset.searchValue||'';
  for(const row of wrapper.querySelectorAll('.search-select-option'))row.setAttribute('aria-selected',String(row===option));
  searchSelectOpen(wrapper,false);
  if(dispatch)hidden.dispatchEvent(new Event('change',{bubbles:true}));
}
function setSearchSelectValue(root,name,value) {
  const wrapper=root.querySelector(`[data-search-select="${name}"]`);
  if(!wrapper)return;
  const option=Array.from(wrapper.querySelectorAll('.search-select-option')).find(row=>row.dataset.searchId===String(value||''));
  searchSelectChoose(wrapper,option,false);
}
function validateSearchSelections(root) {
  for(const input of root.querySelectorAll('.search-select-input'))if(!input.reportValidity())return false;
  return true;
}

document.addEventListener('click',event=>{
  const wrapper=event.target.closest('[data-search-select]');
  for(const other of document.querySelectorAll('[data-search-select]'))if(other!==wrapper)searchSelectOpen(other,false);
  if(!wrapper)return;
  const input=wrapper.querySelector('.search-select-input');
  const option=event.target.closest('.search-select-option');
  if(option){searchSelectChoose(wrapper,option);input.focus();return;}
  if(event.target.closest('.search-select-toggle')){searchSelectFilter(wrapper,'');searchSelectOpen(wrapper,input.getAttribute('aria-expanded')!=='true');input.focus();return;}
  if(event.target===input){searchSelectFilter(wrapper,'');searchSelectOpen(wrapper,true);}
});
document.addEventListener('input',event=>{
  if(!event.target.matches('.search-select-input'))return;
  const input=event.target,wrapper=input.closest('[data-search-select]');
  searchHidden(wrapper).value='';
  const snapshot=wrapper.querySelector('[data-search-snapshot]');if(snapshot)snapshot.value='';
  const newValue=wrapper.querySelector('[data-search-newvalue]');if(newValue)newValue.value='';
  const hint=wrapper.dataset.newLabel?`Select from the list, or choose “＋ Add new ${wrapper.dataset.newLabel}”.`:'Select a registration from the list, or clear this field.';
  input.setCustomValidity(input.value.trim()?hint:'');
  searchSelectFilter(wrapper,input.value);searchSelectOpen(wrapper,true);
});
document.addEventListener('change',event=>{
  if(!event.target.matches('.search-select-input'))return;
  const input=event.target,wrapper=input.closest('[data-search-select]'),query=input.value.trim().toLowerCase();
  const exact=Array.from(wrapper.querySelectorAll('.search-select-option')).find(option=>option.dataset.searchId&&(option.dataset.searchLabel.toLowerCase()===query||option.dataset.searchValue.toLowerCase()===query));
  if(exact)searchSelectChoose(wrapper,exact);
  else if(!query)searchSelectChoose(wrapper,null);
});
document.addEventListener('keydown',event=>{
  if(!event.target.matches('.search-select-input'))return;
  const input=event.target,wrapper=input.closest('[data-search-select]');
  if(event.key==='Escape'||event.key==='Tab'){searchSelectOpen(wrapper,false);return;}
  if(!['ArrowDown','ArrowUp','Enter'].includes(event.key))return;
  if(event.key==='Enter'&&input.getAttribute('aria-expanded')!=='true')return;
  event.preventDefault();
  if(input.getAttribute('aria-expanded')!=='true'){searchSelectFilter(wrapper,input.value);searchSelectOpen(wrapper,true);}
  const options=Array.from(wrapper.querySelectorAll('.search-select-option')).filter(option=>!option.hidden);
  if(!options.length)return;
  const current=options.findIndex(option=>option.classList.contains('search-highlight'));
  if(event.key==='Enter'){searchSelectChoose(wrapper,options[Math.max(0,current)]);return;}
  const index=event.key==='ArrowDown'?(current+1)%options.length:(current<=0?options.length-1:current-1);
  for(const option of options)option.classList.remove('search-highlight');
  options[index].classList.add('search-highlight');input.setAttribute('aria-activedescendant',options[index].id);
  options[index].scrollIntoView({block:'nearest'});
});
document.addEventListener('focusin',event=>{
  for(const wrapper of document.querySelectorAll('[data-search-select]'))if(!wrapper.contains(event.target))searchSelectOpen(wrapper,false);
});

/* Free-text inputs with suggestions: <input data-suggest="plates">.
   Typing still filters by plain text; the list only recommends values.
   Sources are registered by the app in `suggestSources`. */
const suggestSources={};
let suggestMenu=null,suggestInput=null,suggestQuietUntil=0;
function suggestElement(){
  if(!suggestMenu){suggestMenu=document.createElement('div');suggestMenu.className='suggest-menu';suggestMenu.setAttribute('role','listbox');suggestMenu.hidden=true;document.body.append(suggestMenu);}
  return suggestMenu;
}
function suggestHide(){if(suggestMenu)suggestMenu.hidden=true;suggestInput=null;}
function suggestShow(input){
  if(Date.now()<suggestQuietUntil)return;
  const source=suggestSources[input.dataset.suggest];if(!source)return;
  const query=input.value.trim().toLowerCase();
  const seen=new Set(),items=[];
  for(const value of source()){const text=String(value||'').trim(),key=text.toLowerCase();if(!text||seen.has(key)||key===query||!key.includes(query))continue;seen.add(key);items.push(text);}
  items.sort((a,b)=>(b.toLowerCase().startsWith(query)-a.toLowerCase().startsWith(query))||a.localeCompare(b));
  const menu=suggestElement();
  if(!items.length){suggestHide();return;}
  const mark=text=>{if(!query)return esc(text);const at=text.toLowerCase().indexOf(query);return esc(text.slice(0,at))+'<mark>'+esc(text.slice(at,at+query.length))+'</mark>'+esc(text.slice(at+query.length));};
  menu.innerHTML=`<p class="suggest-title">Suggestions · or keep typing</p>`+items.slice(0,8).map(text=>`<button type="button" role="option" class="search-select-option" data-suggest-value="${esc(text)}">${mark(text)}</button>`).join('');
  const rect=input.getBoundingClientRect();
  menu.style.left=rect.left+'px';menu.style.top=(rect.bottom+4)+'px';menu.style.width=Math.max(rect.width,240)+'px';
  menu.hidden=false;suggestInput=input;
}
function suggestPick(value){
  const input=suggestInput;if(!input)return;
  input.value=value;suggestHide();suggestQuietUntil=Date.now()+700;
  input.dispatchEvent(new Event('input',{bubbles:true}));
  input.dispatchEvent(new Event('change',{bubbles:true}));
}
document.addEventListener('focusin',event=>{if(event.target.matches?.('[data-suggest]'))suggestShow(event.target);});
document.addEventListener('input',event=>{if(event.target.matches?.('[data-suggest]'))suggestShow(event.target);});
document.addEventListener('focusout',event=>{if(event.target===suggestInput)setTimeout(()=>{if(document.activeElement!==suggestInput)suggestHide();},120);});
document.addEventListener('mousedown',event=>{const option=event.target.closest?.('[data-suggest-value]');if(option){event.preventDefault();suggestPick(option.dataset.suggestValue);}});
document.addEventListener('keydown',event=>{
  if(!event.target.matches?.('[data-suggest]')||!suggestMenu||suggestMenu.hidden)return;
  const options=[...suggestMenu.querySelectorAll('[data-suggest-value]')],current=options.findIndex(o=>o.classList.contains('search-highlight'));
  if(event.key==='Escape'){suggestHide();return;}
  if(event.key==='Enter'&&current>=0){event.preventDefault();suggestPick(options[current].dataset.suggestValue);return;}
  if(event.key!=='ArrowDown'&&event.key!=='ArrowUp')return;
  event.preventDefault();
  const next=event.key==='ArrowDown'?(current+1)%options.length:(current<=0?options.length-1:current-1);
  options.forEach(o=>o.classList.remove('search-highlight'));options[next].classList.add('search-highlight');options[next].scrollIntoView({block:'nearest'});
});
if(typeof window!=='undefined')window.addEventListener('resize',suggestHide);
document.addEventListener('scroll',event=>{if(suggestInput&&event.target!==suggestMenu)suggestShow(suggestInput);},true);
