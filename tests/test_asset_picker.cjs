'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../static/workspace.js'), 'utf8');

const dialog = {open:true, close(){ this.open = false; }};
const assets = [{id:'first'}, {id:'second'}];
const context = {
  assets,
  assetPickerSelected:new Set(['second']),
  assetPickerResolve:null,
  clearPickerThumbnails(){},
  $:id => id === 'assetPickerDialog' ? dialog : null
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function confirmPicker()'), source.indexOf('  async function openAssetPicker(')), context);

(async () => {
  const confirmed = new Promise(resolve => { context.assetPickerResolve = resolve; });
  context.confirmPicker();
  assert.deepEqual(await confirmed, [assets[1]]);
  assert.equal(dialog.open, false);
  assert.equal(context.assetPickerResolve, null);
  console.log('PASS confirming returns selected assets after closing the dialog');

  dialog.open = true;
  const cancelled = new Promise(resolve => { context.assetPickerResolve = resolve; });
  context.closePicker();
  assert.equal(await cancelled, null);
  assert.equal(dialog.open, false);
  assert.equal(context.assetPickerResolve, null);
  context.closePicker();
  console.log('PASS cancelling resolves once without applying assets');
})().catch(error => { console.error(error); process.exitCode = 1; });
