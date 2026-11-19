package channel

import (
	"context"
	"errors"
	"sync"

	"eagle-eye/xhs/internal/xhs"
)

// noteResult 一篇笔记的详情读取结果（Note 与 Err 二选一；两者都为 nil 表示因整体中止而未读取）。
type noteResult struct {
	Note *xhs.Note
	Err  error
}

// abortsAll 读取单篇笔记时遇到这些错误必须立即停止整个搜索（包括其他并行的标签页）。
func abortsAll(err error) bool {
	return errors.Is(err, xhs.ErrVerification) || errors.Is(err, xhs.ErrLoginRequired) || errors.Is(err, xhs.ErrBrowserClosed)
}

// fetchDetails 最多 n 个并发读取 cards 的详情，结果与 cards 一一对应。
// 任一篇遇到验证页/登录失效/浏览器关闭：取消其余进行中的读取（它们的 ctx 被取消、标签页随之关闭），
// 不再启动新的读取，返回该错误。ctx 本身结束时返回 ctx.Err()。
func fetchDetails(ctx context.Context, cards []xhs.Card, n int,
	fetch func(context.Context, xhs.Card) (*xhs.Note, error)) ([]noteResult, error) {
	n = max(1, n)
	parent := ctx
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	results := make([]noteResult, len(cards))
	sem := make(chan struct{}, n)
	var (
		wg    sync.WaitGroup
		mu    sync.Mutex
		abort error
	)
	stop := func(err error) {
		mu.Lock()
		if abort == nil {
			abort = err
		}
		mu.Unlock()
		cancel()
	}

launch:
	for i, card := range cards {
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			break launch
		}
		if ctx.Err() != nil { // 拿到名额的同时被中止
			<-sem
			break
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			note, err := fetch(ctx, card)
			if err != nil && abortsAll(err) {
				stop(err)
			}
			results[i] = noteResult{Note: note, Err: err}
		}()
	}
	wg.Wait()

	mu.Lock()
	defer mu.Unlock()
	if abort != nil {
		return results, abort
	}
	return results, parent.Err()
}
