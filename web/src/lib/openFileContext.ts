import { createContext } from 'react'

/** "Open this path in the project's Files tab". Provided by ProjectView; null where there is no
 *  Files tab to open it in (free chats), in which case the chat leaves paths as plain text. */
export const OpenFileContext = createContext<((path: string) => void) | null>(null)
