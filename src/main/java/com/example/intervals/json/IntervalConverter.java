package com.example.intervals.json;

import com.example.intervals.engine.Domain;
import com.example.intervals.model.Cut;
import com.example.intervals.model.Interval;

import java.util.ArrayList;
import java.util.List;

/**
 * Converts between {@link IntervalDto} and the typed {@link Interval} model of
 * a given {@link Domain}. Endpoint parsing is the boundary where invalid
 * tokens become {@code invalid_endpoint} errors; reversed values are rejected
 * by {@link Interval#of} as {@code reversed_interval}.
 */
public final class IntervalConverter<T extends Comparable<? super T>> {

    private final Domain<T> domain;

    public IntervalConverter(Domain<T> domain) {
        this.domain = domain;
    }

    public Interval<T> toModel(IntervalDto dto) {
        T low = dto.lower() == null ? null : domain.parseEndpoint(dto.lower());
        T high = dto.upper() == null ? null : domain.parseEndpoint(dto.upper());
        return Interval.between(low, dto.lowerIsOpen(), high, dto.upperIsOpen());
    }

    public List<Interval<T>> toModelList(List<IntervalDto> dtos) {
        List<Interval<T>> out = new ArrayList<>();
        if (dtos != null) {
            for (IntervalDto dto : dtos) {
                out.add(toModel(dto));
            }
        }
        return out;
    }

    public IntervalDto toDto(Interval<T> iv) {
        Cut<T> lo = iv.lowerCut();
        Cut<T> hi = iv.upperCut();
        String low = lo.isInfinite() ? null : domain.formatEndpoint(lo.endpoint());
        String high = hi.isInfinite() ? null : domain.formatEndpoint(hi.endpoint());
        boolean lowerOpen = lo.kind() == Cut.Kind.ABOVE;
        boolean upperOpen = hi.kind() == Cut.Kind.BELOW;
        return new IntervalDto(low, lowerOpen, high, upperOpen);
    }

    public List<IntervalDto> toDtoList(List<Interval<T>> intervals) {
        List<IntervalDto> out = new ArrayList<>();
        for (Interval<T> iv : intervals) {
            out.add(toDto(iv));
        }
        return out;
    }
}
