'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../static/workbench.js'), 'utf8');
const elements = new Map();
const $ = id => {
  assert.notEqual(id, 'promptEnhance', 'retired rewrite control must not be accessed');
  assert.notEqual(id, 'camera', 'retired camera control must not be accessed');
  if (!elements.has(id)) elements.set(id, {value:'',checked:false,disabled:false,tagName:'INPUT',classList:{toggle(){}}});
  return elements.get(id);
};
let draft = null, characters = [];
const context = {
  $, studioMode:'text', references:[], referenceModelActive:false, nonReferencePeople:false,
  window:{H3Characters:{used:()=>characters}},
  localStorage:{getItem:()=>draft,setItem:(key,value)=>{draft=value;}}, DRAFT:'test',
  document:{querySelectorAll:()=>[]},
  updateReleaseUI(){}, updateEnhancement(){}, updateGenerate(){},
  selectedRatio:()=> '16:9', selectedReleaseMode:()=> 'always',
  referenceData:()=>context.references,
  restoreReferences:refs=>{context.references=refs;},
  setMode:mode=>{context.studioMode=mode;context.updateModelControls();}
};
vm.createContext(context);
for (const [start,end] of [
  ['function updatePerformance()', 'function setMode('],
  ['function saveDraft()', 'function updateRunMonitor('],
  ['function generationConfig()', "$('textTab').onclick"]
]) vm.runInContext(source.slice(source.indexOf(start),source.indexOf(end)),context);
$('performance').checked = $('quantizedModel').checked = $('quantizedVae').checked = true;
$('encoder').value = 'heretic_int8';
$('seed').value = '1';
$('mode').tagName = 'SELECT'; $('mode').options = [{value:'pdd'},{value:'base'}];
$('peopleEnhancement').checked = true;
for (const mode of ['pdd','base']) {
  for (const route of ['reference','anchors','character','text']) {
    $('mode').value = mode;
    characters = route === 'character' ? [{id:'test'}] : [];
    context.studioMode = route === 'text' ? 'text' : 'image';
    context.references = route === 'reference' ? [{frame:'reference'}] : route === 'anchors' ? [{frame:'first'},{frame:'last'}] : [];
    context.updateModelControls();
    assert.equal($('mode').disabled,false,route);
    assert.equal($('mode').value,mode,route);
    assert.equal(context.generationConfig().mode,mode,route);
    assert.equal($('peopleEnhancement').disabled,['reference','character'].includes(route));
    context.saveDraft(); assert.equal(JSON.parse(draft).mode,mode);
    $('mode').value = mode === 'pdd' ? 'base' : 'pdd';
    context.restoreDraft(); assert.equal($('mode').value,mode);
  }
}
// Changing a choice while Ref2VA is active must survive leaving that route.
characters = [{id:'test'}]; context.updateModelControls(); $('mode').value = 'base';
characters = []; context.updateModelControls(); assert.equal($('mode').value,'base');
console.log('PASS sampling selection, request payload and draft restore for references, anchors, characters and text');

// New annotation mode has its own persisted preference, independent of retired flags.
draft = JSON.stringify({promptEnhance:false});
context.restoreDraft();
assert.equal($('qwenFormatting').checked,true);
assert.equal(context.generationConfig().prompt_processing,'faithful');
$('qwenFormatting').checked = false;
context.saveDraft();
$('qwenFormatting').checked = true;
context.restoreDraft();
assert.equal($('qwenFormatting').checked,false);
assert.equal(context.generationConfig().prompt_processing,'direct');
console.log('PASS Qwen default, explicit opt-out, payload and draft restore');

// Old drafts must not restore a hidden camera override.
draft = JSON.stringify({...JSON.parse(draft),camera:'orbit',promptEnhance:true});
context.restoreDraft();
assert.equal('camera' in context.generationConfig(),false);
context.saveDraft();
assert.equal('camera' in JSON.parse(draft),false);

assert.equal('prompt_enhance' in context.generationConfig(),false);
assert.equal('promptEnhance' in JSON.parse(draft),false);

// Turning acceleration off forces the original request without losing the saved choices.
$('performance').checked = false;
$('mode').value = 'pdd'; $('veda').checked = true; $('peopleEnhancement').checked = true;
context.updatePerformance();
let original = context.generationConfig();
assert.equal(original.mode,'base'); assert.equal(original.encoder,'official_bf16');
for (const key of ['performance','veda','people','quantized_model','quantized_vae']) assert.equal(original[key],false,key);
assert.equal($('performanceOptions').disabled,true);
context.saveDraft(); $('performance').checked = true; context.restoreDraft();
assert.equal($('performance').checked,false); assert.equal($('mode').value,'pdd');
$('performance').checked = true; context.updatePerformance();
assert.equal(context.generationConfig().mode,'pdd');
assert.equal(context.generationConfig().encoder,'heretic_int8');
assert.equal($('performanceOptions').disabled,false);
console.log('PASS master switch, original request, child preferences and draft persistence');
