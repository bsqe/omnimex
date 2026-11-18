import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { resolveInitialLocale } from './i18n';
import { installTranslateDomPatch } from './translateGuard';

describe('translateGuard & ErrorBoundary', () => {
  it('survives browser translator <font> wrapping during React unmount and DOM updates', () => {
    installTranslateDomPatch();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    function ConditionalToggle() {
      const [showFirst, setShowFirst] = useState(true);
      return (
        <div>
          <button type="button" onClick={() => setShowFirst((v) => !v)}>
            Toggle
          </button>
          <div data-testid="slot">
            {showFirst ? 'Original text' : <span>Switched view</span>}
          </div>
        </div>
      );
    }

    render(<ConditionalToggle />);
    const slot = screen.getByTestId('slot');
    const textNode = slot.firstChild;
    expect(textNode).not.toBeNull();

    // Simulate Chrome/Edge Translate wrapping React's #text node inside nested <font> tags
    const outerFont = document.createElement('font');
    const innerFont = document.createElement('font');
    outerFont.appendChild(innerFont);
    slot.replaceChild(outerFont, textNode!);
    innerFont.appendChild(textNode!);

    // Switching state forces React to remove/replace `textNode` whose parentNode is now <font>
    fireEvent.click(screen.getByRole('button', { name: 'Toggle' }));
    expect(screen.getByText('Switched view')).toBeInTheDocument();
    expect(slot.querySelectorAll('font').length).toBe(0);

    // Also verify detached referenceNode in insertBefore does not throw
    const orphan = document.createTextNode('detached');
    const inserted = document.createElement('span');
    inserted.textContent = 'Inserted safely';
    expect(() => slot.insertBefore(inserted, orphan)).not.toThrow();
    expect(screen.getByText('Inserted safely')).toBeInTheDocument();

    warnSpy.mockRestore();
  });

  it('catches runtime render errors in ErrorBoundary and recovers cleanly on click', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    let shouldThrow = true;

    function MaybeBroken() {
      if (shouldThrow) {
        throw new Error(
          "NotFoundError: Failed to execute 'removeChild' on 'Node': The node to be removed is not a child of this node."
        );
      }
      return <div>Recovered healthy panel</div>;
    }

    render(
      <ErrorBoundary>
        <MaybeBroken />
      </ErrorBoundary>
    );

    expect(screen.getByText('View rendering error caught safely')).toBeInTheDocument();
    expect(screen.getByText(/NotFoundError: Failed to execute 'removeChild'/i)).toBeInTheDocument();
    expect(screen.getByText(/browser page-translation extension/i)).toBeInTheDocument();

    shouldThrow = false;
    fireEvent.click(screen.getByRole('button', { name: 'Recover View' }));
    expect(screen.getByText('Recovered healthy panel')).toBeInTheDocument();

    errSpy.mockRestore();
  });

  it('auto-selects Chinese on zh-CN browsers when localStorage is unset', () => {
    window.localStorage.removeItem('omnimex.locale');
    expect(resolveInitialLocale('zh-CN', true)).toBe('zh');
    expect(resolveInitialLocale('zh-TW', true)).toBe('zh');
    expect(resolveInitialLocale('en-US', true)).toBe('en');
  });
});
