'use strict';
// Makes MalcomTrans installable as an app (opens in its own window, not a new browser tab).
// Nothing is cached: every request goes to the server, so the data is always the current one.
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{if(event.request.method!=='GET'||event.request.mode==='navigate')return;event.respondWith(fetch(event.request));});
