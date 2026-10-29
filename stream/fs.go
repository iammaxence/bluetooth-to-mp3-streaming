package stream

import (
	"context"
	"errors"
	"fmt"
	"github.com/hanwen/go-fuse/v2/fs"
	"github.com/hanwen/go-fuse/v2/fuse"
	"io"
	"os"
	"path"
	"syscall"
)

type (
	Filesystem struct {
		fs.Inode

		DiskImage   string
		BufferQueue *BufferQueue
		FileSize    int64
	}
)

// Filesystem has its files added on OnAdd
var _ = (fs.NodeOnAdder)((*Filesystem)(nil))

// Filesystem can have its files opened
var _ = (fs.NodeOpener)((*Filesystem)(nil))

func (f *Filesystem) OnAdd(ctx context.Context) {
	diskFile, err := os.Open(f.DiskImage)
	if err != nil {
		panic(err)
	}

	stat, err := diskFile.Stat()
	if err != nil {
		panic(err)
	}

	offsetMin := int64(-1)
	offsetMax := int64(-1)

	// Cache sequential reads instead of issuing a system call for every byte.
	block := make([]byte, 1024*1024)
	blockStart := int64(-1)
	blockSize := 0
	for i := int64(0); i+4 <= stat.Size(); i++ {
		if blockStart < 0 || i < blockStart || i+4 > blockStart+int64(blockSize) {
			blockStart = i
			blockSize, err = diskFile.ReadAt(block, i)
			if err != nil && err != io.EOF {
				panic(err)
			}
		}
		curBlock := block[i-blockStart : i-blockStart+4]

		if offsetMin < 0 &&
			curBlock[0] == 0xBA &&
			curBlock[1] == 0xBA &&
			curBlock[2] == 0xBA &&
			curBlock[3] == 0xBA {
			offsetMin = i
			fmt.Printf("[FS] Start offset: %d\n", offsetMin)

			i += f.FileSize
		}

		if offsetMin > 0 && offsetMax < 0 && curBlock[0] != 0xBA {
			offsetMax = i - 1
			fmt.Printf("[FS] End offset: %d\n", offsetMax)
			break
		}
	}

	if offsetMin < 0 || offsetMax < 0 {
		panic(errors.New("cannot find file offsets"))
	}

	fmt.Printf("[FS] File offset: %d to %d\n", offsetMin, offsetMax)

	filename := path.Base(f.DiskImage)
	fmt.Printf("[FS] Adding: %s\n", filename)

	ch := f.NewPersistentInode(ctx, &File{
		original:    diskFile,
		stat:        stat,
		offsetMin:   offsetMin,
		offsetMax:   offsetMax,
		bufferQueue: f.BufferQueue,
	}, fs.StableAttr{})
	f.AddChild(filename, ch, true)
}

func (f *Filesystem) Open(ctx context.Context, openFlags uint32) (fh fs.FileHandle, fuseFlags uint32, errno syscall.Errno) {
	// Disallow writes
	if fuseFlags&(syscall.O_RDWR|syscall.O_WRONLY) != 0 {
		return nil, 0, syscall.EROFS
	}

	// Return FOPEN_DIRECT_IO so content is not cached.
	return fh, fuse.FOPEN_DIRECT_IO, 0
}
