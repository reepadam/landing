(function generateStars() {
  var container = document.getElementById('stars');
  for (var i = 0; i < 220; i++) {
    var star = document.createElement('div');
    star.className = 'star';
    var size = Math.random() * 1.8 + 0.4;
    star.style.width = size + 'px';
    star.style.height = size + 'px';
    star.style.top = Math.random() * 100 + '%';
    star.style.left = Math.random() * 100 + '%';
    star.style.animationDelay = Math.random() * 3 + 's';
    container.appendChild(star);
  }
})();

var audio = document.getElementById('audio');
var startScreen = document.getElementById('start');
var beginBtn = document.getElementById('beginBtn');
var muteBtn = document.getElementById('muteBtn');
var skipBtn = document.getElementById('skipBtn');

var AUDIO_START_MS = 15000;
var AUTO_FORWARD_MS = 100000;
var exited = false;

function begin() {
  startScreen.classList.add('hidden');
  document.body.classList.add('playing');
  setTimeout(function(){
    audio.play().catch(function(err){ console.warn('Audio failed:', err); });
  }, AUDIO_START_MS);
  setTimeout(fadeOutAndExit, AUTO_FORWARD_MS);
}

function exitNow() {
  if (exited) return;
  exited = true;
  window.location.href = '/CV';
}

function fadeOutAndExit() {
  if (exited) return;
  exited = true;
  document.body.classList.add('fading');
  var startVol = audio.volume;
  var steps = 20;
  var i = 0;
  var fade = setInterval(function(){
    i++;
    audio.volume = Math.max(0, startVol * (1 - i / steps));
    if (i >= steps) {
      clearInterval(fade);
      audio.pause();
      window.location.href = '/CV';
    }
  }, 100);
}

beginBtn.addEventListener('click', begin);
muteBtn.addEventListener('click', function(){
  audio.muted = !audio.muted;
  muteBtn.textContent = audio.muted ? 'UNMUTE' : 'MUTE';
});
skipBtn.addEventListener('click', exitNow);
document.addEventListener('keydown', function(e){
  if (e.key === 'Escape') exitNow();
});
