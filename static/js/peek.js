/* Adds a show / hide eye button to every password box on the page. */
(function () {
  var css = '.peek-wrap{position:relative;display:block}' +
    '.peek-wrap>input{padding-right:52px!important;width:100%}' +
    '.peek-toggle{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:38px;height:38px;' +
    'display:flex;align-items:center;justify-content:center;border:none;background:none;border-radius:10px;' +
    'color:var(--text-muted,#5E6F67);cursor:pointer;font-size:16px;padding:0}' +
    '.peek-toggle:hover{background:rgba(47,111,90,.08);color:var(--text-main,#14211B)}' +
    '.peek-toggle:focus-visible{outline:2px solid #2F6F5A;outline-offset:1px}';
  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  function addPeek(input) {
    if (input.dataset.peek) return;
    input.dataset.peek = '1';
    var wrap = document.createElement('div');
    wrap.className = 'peek-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'peek-toggle';
    btn.setAttribute('aria-label', 'Show password');
    btn.title = 'Show password';
    btn.innerHTML = '<i class="fa-solid fa-eye"></i>';
    btn.addEventListener('click', function () {
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.innerHTML = '<i class="fa-solid ' + (show ? 'fa-eye-slash' : 'fa-eye') + '"></i>';
      btn.title = show ? 'Hide password' : 'Show password';
      btn.setAttribute('aria-label', btn.title);
      input.focus();
    });
    wrap.appendChild(btn);
  }

  function run() {
    document.querySelectorAll('input[type="password"]:not([data-no-peek])').forEach(addPeek);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();
