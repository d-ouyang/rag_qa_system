/** mammoth 没有自带类型声明，这里只声明我们用到的 API。 */
declare module 'mammoth' {
  export interface MammothMessage {
    type: string
    message: string
  }
  export interface MammothResult {
    value: string
    messages: MammothMessage[]
  }
  export function convertToHtml(input: { arrayBuffer: ArrayBuffer }): Promise<MammothResult>
}
