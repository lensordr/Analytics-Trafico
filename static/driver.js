// Driver page: the document photos (CMR / albarán). Each photo is made small on the phone (longest side 1800 px,
// JPEG) so it goes through with poor signal, then sent one by one; the page reloads to show what was sent.
(function(){
  const input=document.querySelector('[data-photo]');if(!input)return;
  const label=input.closest('label'),status=document.querySelector('.docs-status');
  // The photo as something a canvas can draw (turned upright): createImageBitmap, or a data: image on older phones
  // (the page's security rules allow data: images, not blob: ones).
  async function picture(file){if(window.createImageBitmap){try{return await createImageBitmap(file,{imageOrientation:'from-image'});}catch{}}
    const url=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);});
    return await new Promise((resolve,reject)=>{const img=new Image();img.onload=()=>resolve(img);img.onerror=reject;img.src=url;});}
  async function shrink(file){const img=await picture(file),w=img.width||img.naturalWidth,h=img.height||img.naturalHeight;
    const scale=Math.min(1,1800/Math.max(w,h)),canvas=document.createElement('canvas');canvas.width=Math.round(w*scale);canvas.height=Math.round(h*scale);
    const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(img,0,0,canvas.width,canvas.height);
    if(img.close)img.close();
    let quality=.72,data=canvas.toDataURL('image/jpeg',quality);
    while(data.length>1900000&&quality>.35){quality-=.12;data=canvas.toDataURL('image/jpeg',quality);}
    return data;}
  async function send(file){const image=await shrink(file);
    const response=await fetch(input.dataset.url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({seq:Number(input.dataset.seq),image})});
    if(!response.ok)throw new Error('upload');}
  input.addEventListener('change',async()=>{const files=[...input.files];if(!files.length)return;
    label.classList.add('busy');status.classList.remove('fail');
    try{for(let i=0;i<files.length;i++){status.textContent=`${input.dataset.busy} ${files.length>1?(i+1)+'/'+files.length:''}`;await send(files[i]);}
      const query=new URLSearchParams(location.search);query.delete('ok');location.replace(location.pathname+'?'+query.toString());}
    catch{status.textContent=input.dataset.fail;status.classList.add('fail');label.classList.remove('busy');input.value='';}});
})();
