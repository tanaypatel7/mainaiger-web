(() => {
  'use strict';
  const root = document.documentElement;
  const theme = document.getElementById('theme');
  const dialog = document.getElementById('entry-dialog');
  const title = document.getElementById('dialog-title');
  const description = document.getElementById('dialog-description');
  const panels = Array.from(document.querySelectorAll('[data-dialog-panel]'));
  const closeButton = document.getElementById('close-dialog');
  const sculpture = document.getElementById('mascot-sculpture');
  const greetingStatus = document.getElementById('greeting-status');
  const look = document.getElementById('mascot-look');
  const flow = document.getElementById('mascot-flow');
  const waveSpeed = document.getElementById('mascot-wave-speed');
  const flowSpeed = document.getElementById('mascot-flow-speed');
  const flowDirection = document.getElementById('mascot-flow-direction');
  const waveLeft = document.getElementById('mascot-wave-left');
  const waveRight = document.getElementById('mascot-wave-right');
  const pause = document.getElementById('mascot-pause');
  const motionNote = document.getElementById('mascot-motion-note');
  let previousFocus = null;
  let nextWave = 'left';
  const content = {
    lab: {
      title: 'Open MainAIger Lab',
      description: 'Your browser opens the workspace. A paired MainAIger runtime supplies your device’s Python environment and resources.'
    },
    setup: {
      title: 'Set up this computer',
      description: 'A preview of the guided path from your first visit to a Lab running on your own resources.'
    },
    signin: {
      title: 'Welcome to MainAIger',
      description: 'Invite-only email sign-in for registered MainAIger users.'
    },
    workspace: {
      title: 'Your starting point',
      description: 'Set up on your own device, or find a space whose owner chooses to share.'
    }
  };
  theme.addEventListener('change', () => {
    root.dataset.theme = ['light', 'dark'].includes(theme.value) ? theme.value : 'system';
  });
  function openPanel(name) {
    if (!Object.hasOwn(content, name)) return;
    if (!dialog.open) previousFocus = document.activeElement;
    title.textContent = content[name].title;
    description.textContent = content[name].description;
    panels.forEach(panel => { panel.hidden = panel.dataset.dialogPanel !== name; });
    if (!dialog.open) dialog.showModal();
    closeButton.focus();
  }
  document.querySelectorAll('[data-open]').forEach(button => {
    button.addEventListener('click', () => openPanel(button.dataset.open));
  });
  document.addEventListener('mainaiger:signin', () => openPanel('workspace'));
  if (new URLSearchParams(window.location.search).get('open') === 'signin') openPanel('signin');
  document.addEventListener('mainaiger:session-ended', () => {
    if (dialog.open && !document.querySelector('[data-dialog-panel="workspace"]').hidden) openPanel('signin');
  });
  closeButton.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
  dialog.addEventListener('close', () => {
    if (previousFocus && document.contains(previousFocus)) previousFocus.focus();
    previousFocus = null;
  });
  function syncMotionControls(state) {
    look.value = state.appearance;
    waveSpeed.value = String(state.waveSpeed);
    flowSpeed.value = String(state.flowSpeed);
    flowDirection.value = state.flowDirection === -1 ? 'ccw' : 'cw';
    flow.setAttribute('aria-pressed', String(state.surfaceFlow));
    flow.textContent = state.surfaceFlow ? 'Stop ribbon flow' : 'Ribbon flow';
    flow.disabled = state.reducedMotion;
    waveLeft.disabled = state.reducedMotion;
    waveRight.disabled = state.reducedMotion;
    pause.disabled = state.reducedMotion;
    pause.textContent = state.paused ? 'Resume motion' : 'Pause motion';
    waveSpeed.disabled = flowSpeed.disabled = flowDirection.disabled = state.reducedMotion;
    motionNote.hidden = !state.reducedMotion && !state.paused;
    motionNote.textContent = state.reducedMotion
      ? 'Your device preference keeps M still.'
      : state.paused ? 'Motion paused. Resume continues from this frame.' : '';
  }
  const rig = new MainManagerRig(sculpture.querySelector('svg'), {onChange: syncMotionControls});
  document.getElementById('greet').addEventListener('click', () => {
    greetingStatus.textContent = rig.getState().reducedMotion
      ? 'Hello from Main Manager. Motion is reduced.'
      : 'Main Manager says hello with a small edge wave.';
    rig.wave(nextWave);
    nextWave = nextWave === 'left' ? 'right' : 'left';
  });
  look.addEventListener('change', () => rig.setAppearance(look.value));
  waveLeft.addEventListener('click', () => rig.wave('left'));
  waveRight.addEventListener('click', () => rig.wave('right'));
  waveSpeed.addEventListener('change', () => rig.setWaveSpeed(Number(waveSpeed.value)));
  flowSpeed.addEventListener('change', () => rig.setFlowSpeed(Number(flowSpeed.value)));
  flowDirection.addEventListener('change', () => rig.setSurfaceFlow(rig.getState().surfaceFlow, flowDirection.value));
  flow.addEventListener('click', () => rig.setSurfaceFlow(!rig.getState().surfaceFlow, flowDirection.value));
  pause.addEventListener('click', () => rig.togglePause());
})();
