"""Chunking strategies for different document processing approaches."""

import asyncio
import re
from abc import ABC, abstractmethod
from typing import Any

from tqdm import tqdm

from app.google_docs.parser import DocumentSection, ParsedDocument
from app.llm.base import LLMProvider
from .models import Chunk, ChunkMetadata


class ChunkingStrategy(ABC):
    """Abstract base class for chunking strategies."""

    @abstractmethod
    async def chunk_document(self, document: ParsedDocument) -> list[Chunk]:
        """Chunk a parsed document into smaller pieces."""
        pass


class BasicChunkingStrategy(ChunkingStrategy):
    """Basic chunking strategy based on sections and character limits."""

    def __init__(
        self, max_chunk_size: int = 1000, overlap_size: int = 100, respect_sections: bool = True
    ):
        """Initialize basic chunking strategy.

        Args:
            max_chunk_size: Maximum characters per chunk
            overlap_size: Characters to overlap between chunks
            respect_sections: Whether to avoid breaking sections across chunks
        """
        self.max_chunk_size = max_chunk_size
        self.overlap_size = overlap_size
        self.respect_sections = respect_sections

    async def chunk_document(self, document: ParsedDocument) -> list[Chunk]:
        """Chunk document using basic size-based strategy."""
        chunks = []
        chunk_index = 0

        print(f"📝 Basic chunking {len(document.sections)} sections...")
        
        # Progress bar for sections
        with tqdm(total=len(document.sections), desc="✂️  Chunking sections", unit="section", ncols=100) as pbar:
            for section in document.sections:
                section_chunks = await self._chunk_section(
                    section, document.document_id, chunk_index, section.tab_title, section.tab_id
                )
                chunks.extend(section_chunks)
                chunk_index += len(section_chunks)
                
                # Update progress
                pbar.set_description(f"✂️  Section: {section.title[:30]}...")
                pbar.update(1)

        # Update total chunk count in metadata
        for chunk in chunks:
            if chunk.metadata:
                chunk.metadata.total_chunks = len(chunks)

        print(f"✅ Basic chunking completed: {len(chunks)} chunks from {len(document.sections)} sections")
        return chunks

    async def _chunk_section(
        self,
        section: DocumentSection,
        document_id: str,
        start_chunk_index: int,
        tab_name: str | None = None,
        tab_id: str | None = None,
    ) -> list[Chunk]:
        """Chunk a single section."""
        chunks = []

        # Get section text
        section_text = section.get_full_text()

        if not section_text.strip():
            return chunks

        # If section is small enough, keep as single chunk
        if len(section_text) <= self.max_chunk_size:
            metadata = ChunkMetadata(
                source_document_id=document_id,
                source_tab=tab_name,
                source_tab_id=tab_id,
                source_section=section.title,
                chunk_index=start_chunk_index,
                start_position=0,
                end_position=len(section_text),
                heading_level=section.level,
                contains_question=self._contains_question(section_text),
                estimated_tokens=len(section_text) // 4,
            )

            chunk = Chunk(content=section_text, metadata=metadata)
            chunks.append(chunk)
            return chunks

        # Split large section into chunks
        chunk_pieces = self._split_text_with_overlap(section_text)

        for i, piece in enumerate(chunk_pieces):
            metadata = ChunkMetadata(
                source_document_id=document_id,
                source_tab=tab_name,
                source_tab_id=tab_id,
                source_section=section.title,
                chunk_index=start_chunk_index + i,
                start_position=0,  # Will be updated later
                end_position=len(piece),
                overlap_before=self.overlap_size if i > 0 else 0,
                overlap_after=self.overlap_size if i < len(chunk_pieces) - 1 else 0,
                heading_level=section.level,
                contains_question=self._contains_question(piece),
                estimated_tokens=len(piece) // 4,
            )

            chunk = Chunk(content=piece, metadata=metadata)
            chunks.append(chunk)

        return chunks

    def _split_text_with_overlap(self, text: str) -> list[str]:
        """Split text into overlapping chunks."""
        chunks = []
        start = 0

        while start < len(text):
            end = min(start + self.max_chunk_size, len(text))

            # Try to break at word boundary
            if end < len(text):
                # Look for last space within reasonable distance
                last_space = text.rfind(" ", start, end)
                if last_space > start + self.max_chunk_size * 0.8:
                    end = last_space

            chunk_text = text[start:end].strip()
            if chunk_text:
                chunks.append(chunk_text)

            # Move start position with overlap
            start = max(start + self.max_chunk_size - self.overlap_size, end)

        return chunks

    def _contains_question(self, text: str) -> bool:
        """Check if text contains question indicators."""
        return bool(re.search(r"\?|what|how|why|when|where|who", text, re.IGNORECASE))


class SmartChunkingStrategy(ChunkingStrategy):
    """LLM-assisted smart chunking strategy."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        max_chunk_size: int = 1500,
        overlap_size: int = 150,
        use_summaries: bool = True,
    ):
        """Initialize smart chunking strategy.

        Args:
            llm_provider: LLM provider for semantic analysis
            max_chunk_size: Maximum characters per chunk
            overlap_size: Characters to overlap between chunks
            use_summaries: Whether to generate summaries for chunks
        """
        self.llm_provider = llm_provider
        self.max_chunk_size = max_chunk_size
        self.overlap_size = overlap_size
        self.use_summaries = use_summaries

        # Fallback to basic strategy
        self.basic_strategy = BasicChunkingStrategy(
            max_chunk_size=max_chunk_size, overlap_size=overlap_size
        )

    async def chunk_document(self, document: ParsedDocument) -> list[Chunk]:
        """Chunk document using LLM-assisted semantic analysis."""
        try:
            print(f"📝 Smart chunking {len(document.sections)} sections...")
            
            # Process sections concurrently in batches
            batch_size = 3  # Process 3 sections at a time to avoid overwhelming the LLM
            all_chunks = []
            
            for i in range(0, len(document.sections), batch_size):
                batch = document.sections[i:i + batch_size]
                batch_num = i // batch_size + 1
                total_batches = (len(document.sections) + batch_size - 1) // batch_size
                
                print(f"🔄 Processing batch {batch_num}/{total_batches} ({len(batch)} sections)")
                
                # Create concurrent tasks for this batch
                tasks = []
                for j, section in enumerate(batch):
                    chunk_index = sum(len(s.content) for s in document.sections[:i+j]) // 1000  # Rough estimate
                    task = self._chunk_section_semantically(
                        section, document.document_id, chunk_index, section.tab_title, section.tab_id
                    )
                    tasks.append(task)
                
                # Execute batch concurrently
                with tqdm(total=len(batch), desc=f"📦 Batch {batch_num}", unit="section", ncols=100) as pbar:
                    batch_results = []
                    for task in asyncio.as_completed(tasks):
                        section_chunks = await task
                        batch_results.append(section_chunks)
                        pbar.update(1)
                
                # Collect results
                for section_chunks in batch_results:
                    all_chunks.extend(section_chunks)
            
            # Generate summaries concurrently if enabled
            if self.use_summaries:
                print(f"📝 Generating summaries for {len(all_chunks)} chunks...")
                summary_tasks = []
                for chunk in all_chunks:
                    if len(chunk.content) > 200:  # Only summarize substantial chunks
                        summary_tasks.append(self._generate_summary(chunk.content))
                    else:
                        summary_tasks.append(asyncio.sleep(0))  # Placeholder for consistency
                
                # Process summaries in batches to avoid overwhelming the LLM
                summary_batch_size = 5
                with tqdm(total=len(all_chunks), desc="📝 Summarizing", unit="chunk", ncols=100) as pbar:
                    for i in range(0, len(summary_tasks), summary_batch_size):
                        batch_tasks = summary_tasks[i:i + summary_batch_size]
                        batch_summaries = await asyncio.gather(*batch_tasks, return_exceptions=True)
                        
                        # Assign summaries to chunks
                        for j, result in enumerate(batch_summaries):
                            chunk_idx = i + j
                            if chunk_idx < len(all_chunks) and not isinstance(result, Exception) and result:
                                all_chunks[chunk_idx].summary = result
                        
                        pbar.update(len(batch_tasks))

            # Update total chunk count
            for chunk in all_chunks:
                if chunk.metadata:
                    chunk.metadata.total_chunks = len(all_chunks)

            print(f"✅ Smart chunking completed: {len(all_chunks)} chunks from {len(document.sections)} sections")
            return all_chunks

        except Exception as e:
            print(f"⚠️  Smart chunking failed: {e}, falling back to basic strategy")
            return await self.basic_strategy.chunk_document(document)

    async def _chunk_section_semantically(
        self,
        section: DocumentSection,
        document_id: str,
        start_chunk_index: int,
        tab_name: str | None = None,
        tab_id: str | None = None,
    ) -> list[Chunk]:
        """Chunk section using semantic analysis."""
        section_text = section.get_full_text()

        if not section_text.strip():
            return []

        # If section is small, use basic strategy
        if len(section_text) <= self.max_chunk_size:
            return await self.basic_strategy._chunk_section(
                section, document_id, start_chunk_index, tab_name, tab_id
            )

        # Use LLM to identify semantic break points
        break_points = await self._find_semantic_breaks(section_text)

        # Split text at semantic break points
        chunks = []
        chunk_texts = self._split_at_break_points(section_text, break_points)

        for i, chunk_text in enumerate(chunk_texts):
            metadata = ChunkMetadata(
                source_document_id=document_id,
                source_tab=tab_name,
                source_section=section.title,
                chunk_index=start_chunk_index + i,
                start_position=0,
                end_position=len(chunk_text),
                heading_level=section.level,
                contains_question=self._contains_question(chunk_text),
                estimated_tokens=len(chunk_text) // 4,
            )

            chunk = Chunk(content=chunk_text.strip(), metadata=metadata)
            chunks.append(chunk)

        return chunks

    async def _find_semantic_breaks(self, text: str) -> list[int]:
        """Use LLM to find good break points in text."""
        try:
            prompt = f"""Analyze this text and identify good break points for chunking into semantic units.
            
Text length: {len(text)} characters
Target chunk size: {self.max_chunk_size} characters

Text:
{text[:2000]}{"..." if len(text) > 2000 else ""}

Return positions (character indices) where natural breaks occur, such as:
- Topic transitions
- End of examples or lists  
- Paragraph boundaries
- Logical conclusion points

Return only numbers separated by commas, e.g.: 150, 450, 750"""

            response = await self.llm_provider.generate_response(prompt)

            if response.success and response.content:
                # Parse break points from response
                break_points = []
                for match in re.findall(r"\d+", response.content):
                    pos = int(match)
                    if 0 < pos < len(text):
                        break_points.append(pos)

                return sorted(break_points)

        except Exception as e:
            print(f"⚠️  Semantic break detection failed: {e}")

        # Fallback to simple paragraph breaks
        return self._find_paragraph_breaks(text)

    def _find_paragraph_breaks(self, text: str) -> list[int]:
        """Find paragraph break points as fallback."""
        breaks = []
        for match in re.finditer(r"\n\s*\n", text):
            breaks.append(match.start())
        return breaks

    def _split_at_break_points(self, text: str, break_points: list[int]) -> list[str]:
        """Split text at specified break points with overlap."""
        if not break_points:
            # No break points, use basic splitting
            return self.basic_strategy._split_text_with_overlap(text)

        chunks = []
        start = 0

        for break_point in break_points:
            if break_point - start > self.max_chunk_size * 0.5:
                chunk = text[start:break_point].strip()
                if chunk:
                    chunks.append(chunk)
                start = max(0, break_point - self.overlap_size)

        # Add final chunk
        if start < len(text):
            final_chunk = text[start:].strip()
            if final_chunk:
                chunks.append(final_chunk)

        return chunks

    async def _generate_summary(self, content: str) -> str | None:
        """Generate a summary for chunk content."""
        try:
            prompt = f"""Summarize this document chunk in 1-2 sentences. Focus on the main topic and key information:

{content[:1000]}{"..." if len(content) > 1000 else ""}"""

            response = await self.llm_provider.summarize(content, max_length=100)

            if response.success and response.content:
                return response.content.strip()

        except Exception as e:
            print(f"⚠️  Summary generation failed: {e}")

        return None

    def _contains_question(self, text: str) -> bool:
        """Check if text contains question indicators."""
        return bool(re.search(r"\?|what|how|why|when|where|who", text, re.IGNORECASE))
