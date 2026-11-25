import { cleanup, fireEvent, render } from '@testing-library/react'

import { TraceDrawerToolbar, TraceDrawerToolbarProps } from './TraceDrawerToolbar'

const props: TraceDrawerToolbarProps = {
    position: { index: 0, total: 3 },
    onPrevious: null,
    onNext: null,
    fullPageHref: '/traces/a',
    onClose: () => {},
}

describe('TraceDrawerToolbar back button', () => {
    afterEach(cleanup)

    it('renders nothing without back', () => {
        const { container } = render(<TraceDrawerToolbar {...props} />)
        expect(container.querySelector('[data-attr="trace-drawer-back"]')).toBeNull()
    })

    it('renders the label and calls onBack', () => {
        const onBack = jest.fn()
        const { container } = render(<TraceDrawerToolbar {...props} back={{ label: 'Back to errors', onBack }} />)
        const button = container.querySelector('[data-attr="trace-drawer-back"]')
        expect(button?.textContent).toContain('Back to errors')
        fireEvent.click(button as Element)
        expect(onBack).toHaveBeenCalledTimes(1)
    })
})
