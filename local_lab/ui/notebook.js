(function(scope){
  'use strict';
  function loadNotebook(content){
    if(typeof content!=='string'||content.length>1048576)throw Error('Choose a notebook smaller than 1 MB.');
    let notebook;try{notebook=JSON.parse(content);}catch{throw Error('This is not valid notebook JSON.');}
    if(!notebook||notebook.nbformat!==4||!Array.isArray(notebook.cells)||notebook.cells.length>100)throw Error('Choose a supported Jupyter version 4 notebook.');
    const code=notebook.cells.filter(cell=>cell&&cell.cell_type==='code');
    if(code.length!==1)throw Error('This preview can open notebooks with exactly one code cell.');
    let source=code[0].source;
    if(Array.isArray(source)&&source.every(s=>typeof s==='string'))source=source.join('');
    if(typeof source!=='string'||source.length>16000)throw Error('The code cell must be text under 16,000 characters.');
    return {notebook,source};
  }
  function saveNotebook(original,source){
    if(typeof source!=='string'||source.length>16000)throw Error('Code must be text under 16,000 characters.');
    const notebook=original?JSON.parse(JSON.stringify(original)):{nbformat:4,nbformat_minor:4,metadata:{kernelspec:{display_name:'Python 3',language:'python',name:'python3'}},cells:[{cell_type:'code',metadata:{},source:[],outputs:[],execution_count:null}]};
    const parsed=loadNotebook(JSON.stringify(notebook));
    const cell=parsed.notebook.cells.find(c=>c.cell_type==='code');
    cell.source=source.match(/[^\n]*\n|[^\n]+$/g)||[];cell.outputs=[];cell.execution_count=null;
    return JSON.stringify(parsed.notebook,null,2)+'\n';
  }
  const api={loadNotebook,saveNotebook};scope.MainaigerNotebook=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
