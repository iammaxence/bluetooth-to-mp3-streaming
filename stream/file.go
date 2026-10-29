package stream

import (
	"context"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"io"
	"os"
	"syscall"
)

type File struct {
	fs.Inode
	fullPath    string
	original    *os.File
	stat        os.FileInfo
	offsetMin   int64
	offsetMax   int64
	bufferQueue *BufferQueue
}

var _ = (fs.NodeGetattrer)((*File)(nil))
var _ = (fs.NodeOpener)((*File)(nil))
var _ = (fs.FileReader)((*File)(nil))

// Getattr preserves the FAT image's finite announced size.
func (f *File) Getattr(ctx context.Context, fh fs.FileHandle, out *fuse.AttrOut) syscall.Errno {
	out.Size = uint64(f.stat.Size())
	return 0
}

// Open disables local file-content caching and rejects writes.
func (f *File) Open(ctx context.Context, flags uint32) (fs.FileHandle, uint32, syscall.Errno) {
	if flags&(syscall.O_RDWR|syscall.O_WRONLY) != 0 {
		return nil, 0, syscall.EROFS
	}
	return f, fuse.FOPEN_DIRECT_IO, 0
}

// Read serves metadata from disk and music from the queue, clipping boundary reads.
func (f *File) Read(ctx context.Context, dest []byte, off int64) (fuse.ReadResult, syscall.Errno) {
	if ctx.Err() != nil {
		return nil, syscall.EINTR
	}
	if off < 0 {
		return nil, syscall.EINVAL
	}
	if off >= f.stat.Size() {
		return fuse.ReadResultData([]byte{}), 0
	}
	if int64(len(dest)) > f.stat.Size()-off {
		dest = dest[:f.stat.Size()-off]
	}
	for used := 0; used < len(dest); {
		position := off + int64(used)
		count := len(dest) - used
		if position >= f.offsetMin && position <= f.offsetMax {
			if int64(count) > f.offsetMax-position+1 {
				count = int(f.offsetMax - position + 1)
			}
			data, err := f.bufferQueue.GetContext(ctx, count)
			if err != nil {
				return nil, syscall.EINTR
			}
			copy(dest[used:used+count], data)
		} else {
			if position < f.offsetMin && int64(count) > f.offsetMin-position {
				count = int(f.offsetMin - position)
			}
			n, err := f.original.ReadAt(dest[used:used+count], position)
			if err != nil && err != io.EOF {
				return nil, syscall.EIO
			}
			if n < count {
				return fuse.ReadResultData(dest[:used+n]), 0
			}
		}
		used += count
	}
	return fuse.ReadResultData(dest), 0
}
