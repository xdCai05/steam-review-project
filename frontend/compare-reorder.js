const ITEM = '[data-reorder-item]';
const HANDLE = '[data-reorder-handle]';
const LIST = '[data-reorder-list]';

function targetAt(list, x, y, source) {
  return [...list.querySelectorAll(ITEM)].find(item => {
    const box = item.getBoundingClientRect();
    return item !== source && x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
  });
}

export function bindComparisonReorder(page, { swap, signal }) {
  let drag;
  let frame;
  let suppressClick = false;

  function track() {
    if (!drag?.preview) return;
    const { x, y, list, preview } = drag;
    const margin = 64;
    const speed = y < margin ? -Math.min(18, (margin - y) / 3) :
      y > innerHeight - margin ? Math.min(18, (y - innerHeight + margin) / 3) : 0;
    if (speed) window.scrollBy(0, speed);

    const target = targetAt(list, x, y, drag.item);
    if (drag.target !== target) {
      drag.target?.classList.remove('is-drop-target');
      drag.target = target;
      drag.target?.classList.add('is-drop-target');
    }
    preview.style.left = `${Math.max(5, Math.min(x + 12, innerWidth - preview.offsetWidth - 5))}px`;
    preview.style.top = `${Math.max(5, Math.min(y + 12, innerHeight - preview.offsetHeight - 5))}px`;
    frame = requestAnimationFrame(track);
  }

  function end() {
    if (!drag) return;
    const { item, target, preview, pointerId, handle } = drag;
    drag = undefined;
    cancelAnimationFrame(frame);
    item.classList.remove('is-drag-source');
    target?.classList.remove('is-drop-target');
    preview?.remove();
    if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
  }

  page.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0 || drag) return;
    const item = event.target.closest(ITEM);
    const handle = item?.querySelector(HANDLE);
    if (!handle) return;
    const onHandle = handle.contains(event.target);
    const cardBody = event.pointerType === 'mouse' && item.matches('.compare-game') && !event.target.closest('button');
    if (!onHandle && !cardBody) return;
    drag = { item, handle, list: item.closest(LIST), pointerId: event.pointerId,
      startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY };
  }, { signal });

  page.addEventListener('pointermove', event => {
    if (drag?.pointerId !== event.pointerId) return;
    drag.x = event.clientX;
    drag.y = event.clientY;
    if (drag.preview || Math.hypot(drag.x - drag.startX, drag.y - drag.startY) < 6) return;
    drag.handle.setPointerCapture(event.pointerId);
    drag.item.classList.add('is-drag-source');
    const preview = document.createElement('div');
    preview.className = 'compare-drag-preview';
    preview.textContent = drag.handle.dataset.reorderName;
    preview.style.setProperty('--game-color', getComputedStyle(drag.item).getPropertyValue('--game-color'));
    preview.setAttribute('aria-hidden', 'true');
    drag.preview = preview;
    page.append(preview);
    track();
  }, { signal });

  window.addEventListener('pointerup', event => {
    if (drag?.pointerId !== event.pointerId) return;
    const { item, list, preview } = drag;
    const target = preview && targetAt(list, event.clientX, event.clientY, item);
    end();
    if (!preview) return;
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
    if (target) swap(item.dataset.reorderItem, target.dataset.reorderItem, list.dataset.reorderList, false);
  }, { signal });

  page.addEventListener('click', event => {
    if (!suppressClick) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, { capture: true, signal });

  page.addEventListener('dragstart', event => {
    if (event.target.closest(ITEM)) event.preventDefault();
  }, { signal });

  window.addEventListener('pointercancel', event => {
    if (drag?.pointerId === event.pointerId) end();
  }, { signal });
  window.addEventListener('blur', end, { signal });
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && drag) end();
    const handle = event.target.closest?.(HANDLE);
    if (!handle || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    event.preventDefault();
    const item = handle.closest(ITEM);
    const list = item.closest(LIST);
    const items = [...list.querySelectorAll(ITEM)];
    const direction = ['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1;
    const target = items[items.indexOf(item) + direction];
    if (target) swap(item.dataset.reorderItem, target.dataset.reorderItem, list.dataset.reorderList, true);
  }, { signal });
  signal.addEventListener('abort', end, { once: true });
}
