import { Flow } from '../Flow'
import { PageHeader } from '../ui'

export function HowItWorks() {
  return (
    <>
      <PageHeader title="How a research run works" subtitle="The steps Titan takes, drawn from the pipeline itself. Open any run to watch these light up as it goes." />
      <Flow />
    </>
  )
}
