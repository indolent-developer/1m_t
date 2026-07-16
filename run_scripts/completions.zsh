#!/usr/bin/env zsh
# Zsh completion for ./1m
# Source this in ~/.zshrc:
#   source /path/to/1m/run_scripts/completions.zsh

_1m_completion() {
    local -a commands
    commands=(
        'monitor:Start the live monitor'
        'scan\ post-market:Run post-market movers scan'
        'scan\ pre-market:Run pre-market movers scan'
        'scan\ pre-market-scalp:Run pre-market scalp scan'
        'prompt:Generate a morning analysis prompt'
        'scheduler:Start the morning routine scheduler'
        'snapshot:Instant account snapshot'
        'eod:End-of-day portfolio report'
        'bot:Start the Telegram bot'
        'price-monitor:Start the standalone price monitor'
        'local:Open local console CLI'
        'help:Show help'
    )

    local state
    local -a words
    words=("${(@)words[2,-1]}")  # strip the ./1m

    case "${words[1]}" in
        prompt)
            local -a opts
            opts=('1:Overnight Thesis Check' '2:Pre-Market Decision Run' '3:Opening Confirmation' '--date:Run date YYYY-MM-DD')
            _describe 'prompt options' opts
            ;;
        scan)
            local -a sub
            sub=('post-market:Post-market movers' 'pre-market:Pre-market movers' 'pre-market-scalp:Pre-market scalp')
            _describe 'scan type' sub
            ;;
        monitor)
            local -a opts
            opts=('--list:Symbol sources (all,portfolio,watchlist,scanners)')
            _describe 'monitor options' opts
            ;;
        --list)
            local -a sources
            sources=('all' 'portfolio' 'watchlist' 'scanners')
            _describe 'list sources' sources
            ;;
        eod)
            local -a opts
            opts=('--telegram:Send report to Telegram')
            _describe 'eod options' opts
            ;;
        help)
            _describe 'commands' commands
            ;;
        *)
            _describe 'commands' commands
            ;;
    esac
}

compdef _1m_completion ./1m
