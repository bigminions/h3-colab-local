'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/workspace.js', 'utf8');
const body = source.slice(source.indexOf('  let queuePlacementResolve ='), source.indexOf('  window.Workspace ='));
function setup(status, observedConsumer = status.consumer) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      open: false, textContent: '', classList: {
        hidden: false, remove() {}, toggle(name, value) { this[name] = value; }
      },
      showModal() { this.open = true; }, close() { this.open = false; }, focus() { this.focused = true; }
    });
    return elements.get(id);
  };
  const requests = [];
  const ctx = vm.createContext({$:element, api: async path => {
    requests.push(path);
    if (path === 'consumer') {
      if (observedConsumer instanceof Error) throw observedConsumer;
      return observedConsumer || {state:'unknown'};
    }
    return status;
  }});
  vm.runInContext(body + '\nglobalThis.choose = chooseQueuePlacement; globalThis.respond = closeQueuePlacementModal;', ctx);
  return {ctx, elements, requests};
}
(async () => {
  for (const choice of ['front', 'back']) {
    const {ctx, elements, requests} = setup({active:'running',managed_gpu:true,compute:{gpu:'A100'},jobs:[{id:'running',app_id:'h3'}],queue:[{id:'queued'}]});
    const pending = ctx.choose({targetAppId:'h3',targetGpu:'A100'});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(elements.get('queuePlacementModal').open, true, 'same app and GPU still offer queue placement');
    assert.equal(elements.get('queuePlacementBack').focused, true);
    assert.equal(elements.get('queuePlacementFront').classList.hidden, false);
    assert.equal(elements.get('queuePlacementFront').disabled, false);
    assert.match(elements.get('queuePlacementHelpText').textContent, /当前任务继续执行/);
    assert.equal(elements.get('queuePlacementSummary').textContent, 'H3 视频正在处理 · 当前排队 1 项');
    assert.match(elements.get('queuePlacementHelpText').textContent, /不包含正在执行的任务/);
    assert.match(elements.get('queuePlacementHelpText').textContent, /其余按入队顺序执行/);
    assert.equal(elements.get('queuePlacementHelp').open, false);
    assert.equal(await ctx.choose({}), null, 'a second click cannot replace a pending choice');
    ctx.respond(choice);
    assert.equal(await pending, choice);
    assert.deepEqual(requests, ['status'], 'choosing placement never releases or interrupts a runtime');
  }
  const idle = setup({active:null,managed_gpu:true,compute:{gpu:'G4'},consumer:{state:'idle'},queue:[]});
  assert.equal(await idle.ctx.choose({targetAppId:'h3',targetGpu:'A100'}), 'back');
  assert.equal(idle.elements.has('queuePlacementModal'), false, 'idle environment switching is handled by the dispatcher');
  assert.deepEqual(idle.requests, ['status', 'consumer'], 'retained environments are checked before offering queue placement');
  for (const cachedState of ['busy', 'unknown', 'blocked']) {
    const stale = setup({active:null,managed_gpu:true,consumer:{state:cachedState},queue:[]}, {state:'idle'});
    assert.equal(await stale.ctx.choose({targetAppId:'qwen_image_2_1',targetGpu:'T4'}), 'back');
    assert.equal(stale.elements.has('queuePlacementModal'), false, 'an idle consumer overrides a stale ' + cachedState + ' snapshot');
  }
  for (const observation of [{state:'unknown'}, new Error('observation unavailable')]) {
    const unknown = setup({active:null,managed_gpu:true,consumer:{state:'busy'},queue:[]}, observation);
    assert.equal(await unknown.ctx.choose({}), 'back');
    assert.equal(unknown.elements.has('queuePlacementModal'), false, 'unconfirmed state is handled by the dispatcher without a misleading queue choice');
  }
  const remoteBusy = setup({active:null,managed_gpu:true,consumer:{state:'idle'},queue:[]}, {state:'busy',app_id:'h3'});
  const remoteChoice = remoteBusy.ctx.choose({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(remoteBusy.elements.get('queuePlacementModal').open, true, 'a confirmed remote task still requires queue placement');
  remoteBusy.ctx.respond('back');
  assert.equal(await remoteChoice, 'back');
  const waiting = setup({active:null,managed_gpu:false,queue:[{id:'queued'}]});
  const pending = waiting.ctx.choose({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(waiting.elements.get('queuePlacementModal').open, true, 'waiting tasks offer placement even before a GPU connects');
  waiting.ctx.respond(false);
  assert.equal(await pending, false);
  const status = {active:'running',managed_gpu:true,consumer:{app_id:'qwen_image_2_1'},queue:[{id:'queued'}]};
  const changing = setup(status);
  let choice = changing.ctx.choose({});
  await new Promise(resolve => setImmediate(resolve));
  changing.ctx.respond('back');
  await choice;
  status.queue = [];
  choice = changing.ctx.choose({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(changing.elements.get('queuePlacementFront').classList.hidden, true, 'no waiting tasks means no front insertion choice');
  assert.equal(changing.elements.get('queuePlacementFront').disabled, true);
  assert.equal(changing.elements.get('queuePlacementBack').textContent, '排队');
  assert.equal(changing.elements.get('queuePlacementSummary').textContent, 'Qwen-Image-2.1正在处理 · 当前排队 0 项');
  assert.match(changing.elements.get('queuePlacementHelpText').textContent, /当前没有其他等待任务，无需插队/);
  changing.ctx.respond('back');
  assert.equal(await choice, 'back');
  console.log('PASS queue choices, empty waiting queue, duplicate click guard and idle switching');
})().catch(error => { console.error(error); process.exitCode = 1; });
