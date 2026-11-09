// hermes-intercom desktop half.
//
// Catalog rule 8 limits a desktop plugin to the plugin SDK. The SDK has panes,
// pages, composer rows, transcript directives and tool-media renderers, but no
// contribution that draws an incoming agent message. This file therefore renders
// nothing: delivery stays in the Python backend and Hermes Desktop shows its own
// native note. register/onDispose exist so the host can load and unload the
// plugin; neither one reads or writes the page.

export default {
  id: 'hermes-intercom',
  name: 'Hermes Intercom',
  description: 'Delivers inter-session messages. Desktop shows the host note unchanged.',
  register(ctx) {
    ctx.onDispose(() => {})
  }
}
