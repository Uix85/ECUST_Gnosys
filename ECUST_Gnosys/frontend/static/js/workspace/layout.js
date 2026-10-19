/* Sidebars, responsive layout and reading preferences. */
'use strict';
const SIDEBARS = {
    nav: {panel:'navigation',handle:'nav-resize',css:'--nav-width',store:'gnosys-navigation-width',cls:'no-navigation',toggle:'toggle-nav'},
    inspector: {panel:'inspector',handle:'panel-resize',css:'--panel-width',store:'gnosys-inspector-width',cls:'no-inspector',toggle:'toggle-panel'}
};
const READING_MIN_WIDTH = 320;
const sidebarWidths = {nav:0, inspector:0};
const mobileLayoutQuery = window.matchMedia('(max-width: 800px)');
let mobileLayout = mobileLayoutQuery.matches;
function pageWidth() { return document.documentElement.clientWidth || window.innerWidth; }
function sidebarMinWidth() { return Math.max(1,Math.ceil(pageWidth()/50)); }
function sidebarMaxWidth() {
    const page=pageWidth();
    const rail=parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--rail-width'))||0;
    // 硬上限：页宽 1/3；同时给左侧功能区、中间阅读区留出最小宽度。
    return Math.max(0,Math.min(Math.floor(page/3),Math.floor((page-rail-READING_MIN_WIDTH)/2)));
}
function applySidebarWidth(kind,width,persist) {
    const cfg=SIDEBARS[kind],panel=$(cfg.panel);if(!panel)return 0;
    const min=sidebarMinWidth(),max=sidebarMaxWidth();if(max<=min)return 0;
    const value=Math.max(min,Math.min(max,Math.round(width)));
    if(pageWidth()>800){document.documentElement.style.setProperty(cfg.css,value+'px');sidebarWidths[kind]=value;}
    const handle=$(cfg.handle);
    if(handle){handle.setAttribute('aria-valuemin',String(min));handle.setAttribute('aria-valuemax',String(max));handle.setAttribute('aria-valuenow',String(value));}
    if(persist)localStorage.setItem(cfg.store,String(value));
    return value;
}
function syncSidebarHandles() {
    const min=sidebarMinWidth(),max=sidebarMaxWidth(),mobile=pageWidth()<=800;
    for(const kind of Object.keys(SIDEBARS)){
        const cfg=SIDEBARS[kind],handle=$(cfg.handle),panel=$(cfg.panel);if(!handle||!panel)continue;
        const enabled=!mobile&&max>min;handle.hidden=!enabled;if(!enabled)continue;
        if(!document.body.classList.contains(cfg.cls)){
            const width=sidebarWidths[kind]||panel.getBoundingClientRect().width;
            applySidebarWidth(kind,width,false);
        }
    }
}
function applyResponsiveLayout(force = false) {
    const mobile = mobileLayoutQuery.matches;
    if (!force && mobile === mobileLayout) return;
    mobileLayout = mobile;
    if (mobile) {
        document.body.classList.add('no-navigation', 'no-inspector');
        $('toggle-nav').setAttribute('aria-expanded', 'false');
        $('toggle-panel').setAttribute('aria-expanded', 'false');
    } else {
        document.body.classList.remove('no-navigation', 'no-inspector');
        $('toggle-nav').setAttribute('aria-expanded', 'true');
        $('toggle-panel').setAttribute('aria-expanded', 'true');
    }
    syncSidebarHandles();
}
function setSidebarVisible(kind,visible) {
    const cfg=SIDEBARS[kind],panel=$(cfg.panel),toggle=$(cfg.toggle);if(!panel)return;
    if(!visible){
        const width=panel.getBoundingClientRect().width;if(width>0)sidebarWidths[kind]=Math.round(width);
        document.body.classList.add(cfg.cls);
    }else{
        document.body.classList.remove(cfg.cls);
        const stored=parseFloat(localStorage.getItem(cfg.store));
        const width=Number.isFinite(stored)?stored:(sidebarWidths[kind]||panel.getBoundingClientRect().width);
        applySidebarWidth(kind,width,false);
    }
    if(toggle)toggle.setAttribute('aria-expanded',String(visible));
    syncSidebarHandles();
}
function toggleSidebar(kind) { const cfg=SIDEBARS[kind];setSidebarVisible(kind,document.body.classList.contains(cfg.cls)); }
function startSidebarDrag(kind,event) {
    const cfg=SIDEBARS[kind],panel=$(cfg.panel);if(!panel||event.button!==0||pageWidth()<=800)return;
    const min=sidebarMinWidth(),max=sidebarMaxWidth();if(max<=min)return;
    event.preventDefault();
    const startX=event.clientX,startWidth=panel.getBoundingClientRect().width,direction=kind==='nav'?1:-1;
    let done=false;
    const finish=save=>{
        if(done)return;done=true;
        document.body.classList.remove('panel-resizing');
        document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);document.removeEventListener('pointercancel',up);window.removeEventListener('blur',up);
        if(save)applySidebarWidth(kind,panel.getBoundingClientRect().width,true);
    };
    const move=ev=>{
        if(done)return;
        const width=startWidth+(ev.clientX-startX)*direction;
        if(width<min){finish(false);setSidebarVisible(kind,false);return;}
        applySidebarWidth(kind,width,false);
    };
    const up=()=>finish(true);
    document.body.classList.add('panel-resizing');
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);document.addEventListener('pointercancel',up);window.addEventListener('blur',up);
}
function initSidebars() {
    for(const kind of Object.keys(SIDEBARS)){
        const cfg=SIDEBARS[kind],panel=$(cfg.panel),handle=$(cfg.handle);if(!panel||!handle)continue;
        sidebarWidths[kind]=Math.round(panel.getBoundingClientRect().width);
        const stored=parseFloat(localStorage.getItem(cfg.store));
        if(Number.isFinite(stored))applySidebarWidth(kind,stored,false);
        handle.addEventListener('pointerdown',e=>startSidebarDrag(kind,e));
        handle.addEventListener('keydown',e=>{
            if(!['ArrowLeft','ArrowRight'].includes(e.key))return;
            e.preventDefault();
            const current=panel.getBoundingClientRect().width,min=sidebarMinWidth();
            const grow=kind==='nav'?(e.key==='ArrowRight'?1:-1):(e.key==='ArrowLeft'?1:-1);
            const next=current+grow*16;
            if(next<min){setSidebarVisible(kind,false);return;}
            applySidebarWidth(kind,next,true);
        });
    }
    window.addEventListener('resize',syncSidebarHandles);
    window.addEventListener('resize',()=>applyResponsiveLayout());
    if(mobileLayoutQuery.addEventListener)mobileLayoutQuery.addEventListener('change',()=>applyResponsiveLayout());
    else if(mobileLayoutQuery.addListener)mobileLayoutQuery.addListener(()=>applyResponsiveLayout());
    syncSidebarHandles();
}
// Shared navigation for evidence links emitted by the model and generated notes.

function initReadingCycles(){
    const closeMenus=()=>document.querySelectorAll('.reading-pref.open').forEach(wrap=>{
        wrap.classList.remove('open');
        wrap.querySelector('.reading-pref-button')?.setAttribute('aria-expanded','false');
    });
    const controls=[
        {id:'font-size-cycle',key:'font-size',css:'--reading-size',values:['16','18','20','22','24'],fallback:'18',unit:'px',label:'字号',format:v=>v+'px'},
        {id:'line-height-cycle',key:'line-height',css:'--reading-line',values:['1.6','1.9','2.2'],fallback:'1.9',unit:'',label:'行距',format:v=>v}
    ];
    controls.forEach(cfg=>{
        const btn=$(cfg.id);if(!btn)return;
        const wrap=btn.closest('.reading-pref'),menu=wrap?.querySelector('.reading-pref-menu');
        if(!wrap||!menu)return;
        let value=localStorage.getItem('gnosys-'+cfg.key)||cfg.fallback;
        if(!cfg.values.includes(value))value=cfg.fallback;
        const options=new Map();
        const apply=v=>{
            value=v;localStorage.setItem('gnosys-'+cfg.key,v);
            document.documentElement.style.setProperty(cfg.css,v+cfg.unit);
            btn.textContent=cfg.label+' '+cfg.format(v);
            btn.title=cfg.label+'：'+cfg.format(v);
            btn.setAttribute('aria-label',cfg.label+'：'+cfg.format(v));
            options.forEach((opt,val)=>opt.setAttribute('aria-selected',String(val===v)));
        };
        cfg.values.forEach(v=>{
            const opt=el('button','reading-pref-option',cfg.format(v));opt.type='button';
            opt.setAttribute('role','option');opt.dataset.value=v;
            opt.onclick=e=>{e.stopPropagation();apply(v);closeMenus();btn.focus();};
            options.set(v,opt);menu.append(opt);
        });
        apply(value);
        btn.onclick=e=>{
            e.stopPropagation();
            const wasOpen=wrap.classList.contains('open');
            closeMenus();
            if(!wasOpen){wrap.classList.add('open');btn.setAttribute('aria-expanded','true');}
        };
    });
    document.addEventListener('click',closeMenus);
    document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMenus();});
}
function setSemanticVisible(visible){
    document.body.classList.toggle('hide-semantic',!visible);
    const btn=$('toggle-semantic-rail');
    if(btn){btn.setAttribute('aria-pressed',String(visible));btn.title='段落旁注：'+(visible?'开':'关');btn.setAttribute('aria-label','段落旁注：'+(visible?'开':'关'));}
}
