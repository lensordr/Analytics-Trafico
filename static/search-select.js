'use strict';

function searchSelectField(label,name,entries,value,settings={}) {
  const selected=entries.find(entry=>String(entry.id)===String(value));
  const empty=settings.empty||'Unassigned';
  const option=(entry,index)=>`<button type="button" role="option" id="search-${name}-${index}" class="search-select-option" data-search-id="${esc(entry.id)}" data-search-label="${esc(entry.label)}" data-search-value="${esc(entry.value??entry.label)}" data-search-terms="${esc((entry.label+' '+(entry.hint||'')).toLowerCase())}" aria-selected="${String(entry.id)===String(value)}"><strong>${esc(entry.label)}</strong>${entry.hint?`<small>${esc(entry.hint)}</small>`:''}</button>`;
  return `<div class="field"><label for="f-${name}">${esc(label)}</label><div class="search-select" data-search-select="${name}"><input id="f-${name}" class="search-select-input" type="text" role="combobox" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="false" aria-controls="search-options-${name}" autocomplete="off" placeholder="Search registrations…" value="${esc(selected?.label||'')}" ${settings.required?'required':''}><input type="hidden" name="${name}" value="${esc(selected?.id||'')}">${settings.snapshotName?`<input type="hidden" name="${settings.snapshotName}" data-search-snapshot value="${esc(selected?.value??selected?.label??'')}">`:''}<button type="button" class="search-select-toggle" aria-label="Show ${esc(label.toLowerCase())} options">⌄</button><div class="search-select-menu" role="listbox" id="search-options-${name}" hidden>${option({id:'',label:empty,value:''},0)}${entries.map((entry,index)=>option(entry,index+1)).join('')}<p class="search-no-results" hidden>No matching registration. Add it to the directory first.</p></div></div></div>`;
}

function trailerPicker(label,id,plate='') {
  const selected=id??(state.trailers||[]).find(trailer=>trailer.plate.toLowerCase()===String(plate).toLowerCase())?.id;
  const entries=(state.trailers||[]).filter(t=>t.active||t.id===Number(selected)).map(trailer=>{
    const assigned=state.trucks.filter(truck=>truck.trailer_id===trailer.id).map(truck=>truck.plate);
    return {id:trailer.id,label:trailer.plate,value:trailer.plate,hint:[trailer.active?'':'Inactive',assigned.length?'Default for '+assigned.join(', '):'No default tractor',trailer.notes].filter(Boolean).join(' · ')};
  });
  return searchSelectField(label,'trailer_id',entries,selected,{snapshotName:'trailer',empty:'No trailer assigned'});
}

function searchSelectOpen(wrapper,open=true) {
  wrapper.querySelector('.search-select-menu').hidden=!open;
  wrapper.querySelector('.search-select-input').setAttribute('aria-expanded',String(open));
  if(!open)wrapper.querySelector('.search-select-input').removeAttribute('aria-activedescendant');
}
function searchSelectFilter(wrapper,query) {
  query=query.trim().toLowerCase();
  let visible=0;
  for(const option of wrapper.querySelectorAll('.search-select-option')) {
    option.hidden=!!query&&!option.dataset.searchTerms.includes(query);
    option.classList.remove('search-highlight');
    if(!option.hidden)visible++;
  }
  wrapper.querySelector('.search-no-results').hidden=visible>0;
}
function searchSelectChoose(wrapper,option,dispatch=true) {
  const input=wrapper.querySelector('.search-select-input');
  const hidden=wrapper.querySelector('input[type=hidden]');
  hidden.value=option?.dataset.searchId||'';
  input.value=hidden.value?option.dataset.searchLabel:'';
  input.setCustomValidity('');
  const snapshot=wrapper.querySelector('[data-search-snapshot]');
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
  wrapper.querySelector('input[type=hidden]').value='';
  const snapshot=wrapper.querySelector('[data-search-snapshot]');if(snapshot)snapshot.value='';
  input.setCustomValidity(input.value.trim()?'Select a registration from the list, or clear this field.':'');
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
