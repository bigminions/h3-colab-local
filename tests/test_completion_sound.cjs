'use strict';
const assert = require('node:assert/strict');
const CompletionSound = require('../static/completion-sound.js');
function storage() {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value)};
}
function monitor(store = storage(), notifier = null) {
  const sound = new CompletionSound(store, () => null, notifier);
  let count = 0, errorCount = 0;
  sound.play = () => { if (sound.enabled) count++; };
  sound.playError = () => { if (sound.enabled) errorCount++; };
  return {sound, count: () => count, errorCount: () => errorCount};
}
const running = {id:'job-1',status:'running'};
const done = {...running,status:'done',file:'job-1.mp4'};
const failedJob = {id:'job-err-1',status:'error',failure_reason:'CUDA out of memory'};
let passed = 0;
function test(name, run) { run(); passed++; console.log('PASS ' + name); }

test('historical completed jobs stay silent on page load', () => {
  const m = monitor(); m.sound.update([done]); m.sound.update([done]); assert.equal(m.count(),0);
});
test('batch rings once only after all variants are saved', () => {
  const m = monitor(); m.sound.update([running]);
  const first = {...done,batch_id:'batch-1',batch_complete:false};
  m.sound.update([first,{id:'job-2',status:'running',batch_id:'batch-1'}]);
  assert.equal(m.count(),0);
  const finished = [first,{...first,id:'job-2'}].map(j => ({...j,batch_complete:true}));
  m.sound.update(finished); m.sound.update(finished.slice(1)); m.sound.update(finished);
  assert.equal(m.count(),1);
});
test('newly saved completion rings once despite repeated polling and history selection', () => {
  const m = monitor(); m.sound.update([running]); m.sound.update([done]); m.sound.update([done]);
  m.sound.update([]); m.sound.update([done]); assert.equal(m.count(),1);
});
test('failed, unsaved and still releasing jobs stay silent; saved warnings notify', () => {
  const m = monitor(); m.sound.update([running]);
  m.sound.update([{...running,status:'error'}]); m.sound.update([{...running,file:'job-1.mp4'}]);
  m.sound.update([{...running,status:'done'}]); assert.equal(m.count(),0);
  m.sound.update([{...done,status:'warning'}]); assert.equal(m.count(),1);
});
test('mute persists and enabling later does not replay missed completions', () => {
  const store = storage(), m = monitor(store); m.sound.setEnabled(false);
  assert.equal(new CompletionSound(store, () => null).enabled,false);
  m.sound.update([running]); m.sound.update([done]); assert.equal(m.count(),0);
  m.sound.setEnabled(true); m.sound.update([done]); assert.equal(m.count(),0);
  m.sound.update([done,{...done,id:'job-2'}]); assert.equal(m.count(),1);
});
test('shared browser storage avoids duplicate alerts across panels and reloads', () => {
  const store = storage(), a = monitor(store), b = monitor(store);
  a.sound.update([running]); b.sound.update([running]); a.sound.update([done]); b.sound.update([done]);
  assert.equal(a.count()+b.count(),1);
  const c = monitor(store); c.sound.update([running]); c.sound.update([done]); assert.equal(c.count(),0);
});
test('restricted storage and unsupported audio cannot break job polling', () => {
  const unavailable = {getItem: () => {throw Error('blocked');},setItem: () => {throw Error('blocked');}};
  const m = monitor(unavailable); m.sound.update([running]); m.sound.update([done]); m.sound.update([done]);
  assert.equal(m.count(),1);
  const sound = new CompletionSound(storage(), () => {throw Error('audio blocked');});
  assert.doesNotThrow(() => {sound.unlock(); sound.update([running]); sound.update([done]);});
});
test('unlocked audio schedules a short chime and cleans up its nodes', () => {
  const tones = [], gains = [];
  const parameter = {setValueAtTime(){},linearRampToValueAtTime(){},exponentialRampToValueAtTime(){}};
  const context = {state:'running',currentTime:1,destination:{},
    createOscillator(){const tone = {frequency:parameter,connect(){},start(t){this.startAt=t;},stop(t){this.stopAt=t;},disconnect(){this.closed=true;}}; tones.push(tone); return tone;},
    createGain(){const gain = {gain:parameter,connect(){},disconnect(){this.closed=true;}}; gains.push(gain); return gain;}};
  let created = 0; const sound = new CompletionSound(storage(), () => {created++; return context;});
  sound.update([running]); assert.equal(created,0);
  sound.unlock(); sound.unlock(); assert.equal(created,1); sound.update([done]);
  assert.ok(tones.length>0);
  for (const tone of tones) { assert.ok(tone.stopAt>tone.startAt && tone.stopAt-tone.startAt<1); tone.onended(); assert.equal(tone.closed,true); }
  assert.ok(gains.every(g => g.closed));
});
test('Drive upload stays silent until verified local download completes', () => {
  const m = monitor(storage());
  m.sound.update([running]);
  const cloud = {...done, file:null, drive:{file_id:'test-file'}};
  m.sound.update([cloud]); m.sound.update([cloud]);
  assert.equal(m.count(),0);
  const local = {...cloud, file:'test.mp4', local_file:true};
  m.sound.update([local]); m.sound.update([local]);
  assert.equal(m.count(),1);
});
test('batch waits for failed local downloads to be retried', () => {
  const m = monitor(); m.sound.update([running]);
  const first = {...done, batch_id:'batch', batch_complete:true};
  const missing = {...first, id:'job-2', file:null, local_file:false, status:'warning', local_download_error:'interrupted'};
  m.sound.update([first,missing]);
  assert.equal(m.count(),0);
  m.sound.update([first,{...missing,file:'job-2.mp4',local_file:true,local_download_error:null,status:'done'}]);
  assert.equal(m.count(),1);
});

// New tests for failure sound, volume level, and desktop notifications:
test('historical failed jobs stay silent on initial page load', () => {
  const m = monitor();
  m.sound.update([failedJob]);
  m.sound.update([failedJob]);
  assert.equal(m.errorCount(), 0);
  assert.equal(m.count(), 0);
});

test('newly failed jobs play error sound once despite repeated polling', () => {
  const m = monitor();
  m.sound.update([running]);
  m.sound.update([failedJob]);
  m.sound.update([failedJob]);
  m.sound.update([]);
  m.sound.update([failedJob]);
  assert.equal(m.errorCount(), 1);
  assert.equal(m.count(), 0);
});

test('shared browser storage avoids duplicate error alerts across panels and reloads', () => {
  const store = storage(), a = monitor(store), b = monitor(store);
  a.sound.update([running]); b.sound.update([running]);
  a.sound.update([failedJob]); b.sound.update([failedJob]);
  assert.equal(a.errorCount() + b.errorCount(), 1);
  const c = monitor(store);
  c.sound.update([running]); c.sound.update([failedJob]);
  assert.equal(c.errorCount(), 0);
});

test('failed local download triggers error sound once', () => {
  const m = monitor();
  m.sound.update([running]);
  const downloadFailed = {...running, id:'job-dl-err', status:'warning', file:null, local_download_error:'network timeout'};
  m.sound.update([downloadFailed]);
  m.sound.update([downloadFailed]);
  assert.equal(m.errorCount(), 1);
  assert.equal(m.count(), 0);
});

test('both completion and error tones have elevated volume (gain peak >= 0.3)', () => {
  const peakGains = [];
  const toneTypes = [];
  const gainParameter = {
    setValueAtTime() {},
    linearRampToValueAtTime(val) { peakGains.push(val); },
    exponentialRampToValueAtTime() {}
  };
  const freqParameter = { setValueAtTime() {} };
  const context = {
    state: 'running', currentTime: 1, destination: {},
    createOscillator() {
      const tone = { frequency: freqParameter, type: 'sine', connect() {}, start() {}, stop() {}, disconnect() {} };
      toneTypes.push(tone);
      return tone;
    },
    createGain() { return { gain: gainParameter, connect() {}, disconnect() {} }; }
  };
  const sound = new CompletionSound(storage(), () => context);
  sound.unlock();
  sound.play(); // success chime
  assert.ok(peakGains.length >= 3);
  assert.ok(peakGains.every(g => g >= 0.3));

  peakGains.length = 0;
  toneTypes.length = 0;
  sound.playError(); // error alert
  assert.ok(peakGains.length >= 3);
  assert.ok(peakGains.every(g => g >= 0.3));
  assert.ok(toneTypes.some(t => t.type === 'triangle'));
});

test('unlock requests notification permission when permission is default', () => {
  let requested = false;
  const mockNotifier = {
    permission: 'default',
    requestPermission: async () => { requested = true; return 'granted'; }
  };
  const sound = new CompletionSound(storage(), () => null, mockNotifier);
  sound.unlock();
  assert.equal(requested, true);
});

test('web notification fires for completion and error with descriptive details', () => {
  const notifications = [];
  class MockNotification {
    constructor(title, options) {
      notifications.push({ title, options });
    }
    static permission = 'granted';
    static requestPermission = async () => 'granted';
  }
  const m = monitor(storage(), MockNotification);
  m.sound.update([running]);

  // Completion notification
  m.sound.update([{...done, creation: {prompt: 'a cinematic sunset over the ocean'}}]);
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].title, 'H3 视频生成完成');
  assert.ok(notifications[0].options.body.includes('cinematic sunset'));

  // Error notification
  m.sound.update([failedJob]);
  assert.equal(notifications.length, 2);
  assert.equal(notifications[1].title, 'H3 视频生成失败');
  assert.ok(notifications[1].options.body.includes('CUDA out of memory'));
});

console.log(`${passed} completion-sound checks passed`);
