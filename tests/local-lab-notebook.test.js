'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {loadNotebook,saveNotebook}=require('../local_lab/ui/notebook.js');
test('new notebook is standard version 4 and retains source on round trip',()=>{
  const text=saveNotebook(null,'print(1)\nprint(2)\n');const parsed=loadNotebook(text);
  assert.equal(parsed.notebook.nbformat,4);assert.equal(parsed.source,'print(1)\nprint(2)\n');
  assert.deepEqual(parsed.notebook.cells[0].outputs,[]);
});
test('saved copy preserves non-code content and clears stale output',()=>{
  const n={nbformat:4,nbformat_minor:4,metadata:{custom:'keep'},cells:[{cell_type:'markdown',source:['hello'],metadata:{}},{cell_type:'code',source:['old'],metadata:{tag:'keep'},execution_count:1,outputs:[{text:'old'}]}]};
  const result=JSON.parse(saveNotebook(n,'new'));
  assert.equal(result.metadata.custom,'keep');assert.equal(result.cells[0].source[0],'hello');assert.equal(result.cells[1].metadata.tag,'keep');assert.deepEqual(result.cells[1].outputs,[]);
  assert.equal(n.cells[1].outputs.length,1,'original input stays untouched');
});
test('multiple cells, malformed JSON and oversize inputs are rejected',()=>{
  assert.throws(()=>loadNotebook('bad'));assert.throws(()=>loadNotebook('x'.repeat(1048577)));
  assert.throws(()=>loadNotebook(JSON.stringify({nbformat:4,cells:[{cell_type:'code',source:'a'},{cell_type:'code',source:'b'}]})));
  assert.throws(()=>saveNotebook(null,'x'.repeat(16001)));
});
