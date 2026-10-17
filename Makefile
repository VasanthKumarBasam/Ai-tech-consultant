.PHONY: up test crawl pilot eval
up:      ; ./start.sh
test:    ; ./start.sh test
pilot:   ; ./start.sh crawl 20
crawl:   ; ./start.sh crawl
eval:    ; ./start.sh eval
