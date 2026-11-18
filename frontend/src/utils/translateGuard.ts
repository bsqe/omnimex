/**
 * Guard DOM mutations against browser built-in page translation extensions
 * (such as Chrome / Edge Translate) that wrap or replace React-managed
 * `#text` nodes with `<font>` elements, which otherwise trigger fatal
 * `NotFoundError: Failed to execute 'removeChild' / 'insertBefore' on 'Node'`
 * crashes and blank the entire UI.
 */
export function installTranslateDomPatch(): void {
  if (typeof Node === 'undefined' || !Node.prototype) {
    return;
  }
  const proto = Node.prototype as Node & { __omnimexTranslatePatched?: boolean };
  if (proto.__omnimexTranslatePatched) {
    return;
  }
  proto.__omnimexTranslatePatched = true;

  const origRemoveChild = Node.prototype.removeChild;
  const origInsertBefore = Node.prototype.insertBefore;

  function findDirectFontWrapper(parent: Node, target: Node): Node | null {
    let cur: Node | null = target.parentNode;
    while (cur && cur.parentNode && cur.parentNode !== parent) {
      cur = cur.parentNode;
    }
    if (cur && cur.parentNode === parent && cur.nodeName === 'FONT') {
      return cur;
    }
    return null;
  }

  Node.prototype.removeChild = function <T extends Node>(child: T): T {
    if (child.parentNode !== this) {
      try {
        const directWrapper = findDirectFontWrapper(this, child);
        if (directWrapper && child.parentNode) {
          origRemoveChild.call(child.parentNode, child);
          if (directWrapper.childNodes.length === 0 && directWrapper.parentNode === this) {
            origRemoveChild.call(this, directWrapper);
          }
          return child;
        }
      } catch {
        // Fallback safely below
      }
      if (typeof console !== 'undefined' && console.warn) {
        console.warn(
          '[omnimex] Ignored external DOM removeChild mismatch (likely browser page translation).'
        );
      }
      return child;
    }
    return origRemoveChild.call(this, child) as T;
  };

  Node.prototype.insertBefore = function <T extends Node>(
    newNode: T,
    referenceNode: Node | null
  ): T {
    if (referenceNode && referenceNode.parentNode !== this) {
      try {
        const directWrapper = findDirectFontWrapper(this, referenceNode);
        if (directWrapper) {
          return origInsertBefore.call(this, newNode, directWrapper) as T;
        }
        return origInsertBefore.call(this, newNode, null) as T;
      } catch {
        // Fallback safely below
      }
      if (typeof console !== 'undefined' && console.warn) {
        console.warn(
          '[omnimex] Ignored external DOM insertBefore mismatch (likely browser page translation).'
        );
      }
      return newNode;
    }
    return origInsertBefore.call(this, newNode, referenceNode) as T;
  };
}
