package com.portfolio.recall.review;

import java.util.List;
import java.util.Optional;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ReviewItemRepository extends JpaRepository<ReviewItem, Long> {

    /** The queue, oldest first. FIFO is what makes the labelled prefix a valid stopping point. */
    List<ReviewItem> findBySelectedTrueAndSupportedIsNullOrderByIdAsc(Pageable page);

    Optional<ReviewItem> findFirstBySelectedTrueAndSupportedIsNullOrderByIdAsc();

    List<ReviewItem> findByIdLessThanOrderByIdAsc(long id);

    List<ReviewItem> findAllByOrderByIdAsc();

    long countByIdLessThan(long id);

    long countBySelectedTrue();

    long countBySelectedTrueAndSupportedIsNotNull();
}
