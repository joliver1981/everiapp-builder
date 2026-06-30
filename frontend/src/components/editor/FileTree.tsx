import { useState } from 'react'
import { ChevronRight, ChevronDown, File, Folder, FolderOpen } from 'lucide-react'
import { cn } from '@/lib/utils'

interface FileNode {
  name: string
  path: string
  type: 'file' | 'directory'
  children: FileNode[]
}

interface FileTreeProps {
  files: FileNode[]
  selectedFile: string | null
  onSelectFile: (path: string) => void
}

export function FileTree({ files, selectedFile, onSelectFile }: FileTreeProps) {
  return (
    <div className="h-full overflow-y-auto py-2">
      {files.length === 0 ? (
        <p className="px-4 text-xs text-muted-foreground">No files yet</p>
      ) : (
        files.map((node) => (
          <FileTreeNode
            key={node.path}
            node={node}
            depth={0}
            selectedFile={selectedFile}
            onSelectFile={onSelectFile}
          />
        ))
      )}
    </div>
  )
}

function FileTreeNode({
  node,
  depth,
  selectedFile,
  onSelectFile,
}: {
  node: FileNode
  depth: number
  selectedFile: string | null
  onSelectFile: (path: string) => void
}) {
  const [isOpen, setIsOpen] = useState(depth < 2) // Auto-expand first 2 levels
  const isDir = node.type === 'directory'
  const isSelected = node.path === selectedFile

  const iconSize = 14

  return (
    <div>
      <button
        onClick={() => {
          if (isDir) {
            setIsOpen(!isOpen)
          } else {
            onSelectFile(node.path)
          }
        }}
        className={cn(
          'flex w-full items-center gap-1.5 px-2 py-1 text-left text-xs transition-colors',
          isSelected
            ? 'bg-primary/10 text-primary'
            : 'text-muted-foreground hover:bg-accent hover:text-foreground'
        )}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        {isDir ? (
          <>
            {isOpen ? (
              <ChevronDown size={12} className="shrink-0" />
            ) : (
              <ChevronRight size={12} className="shrink-0" />
            )}
            {isOpen ? (
              <FolderOpen size={iconSize} className="shrink-0 text-yellow-500" />
            ) : (
              <Folder size={iconSize} className="shrink-0 text-yellow-500" />
            )}
          </>
        ) : (
          <>
            <span className="w-3" />
            <File size={iconSize} className="shrink-0" />
          </>
        )}
        <span className="truncate">{node.name}</span>
      </button>
      {isDir && isOpen && node.children.map((child) => (
        <FileTreeNode
          key={child.path}
          node={child}
          depth={depth + 1}
          selectedFile={selectedFile}
          onSelectFile={onSelectFile}
        />
      ))}
    </div>
  )
}
